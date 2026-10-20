package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"golang.org/x/net/proxy"
)

type Event struct {
	Key, Group, Title, Detail, Severity string
	Links                               []Link
	State                               int // 0 resolved, 1 firing, 2 unknown/maintenance: does not resolve an incident
	Stamp                               int64
}
type Incident struct {
	ID                                                                                   int64
	Group, Title, Severity, Detail                                                       string
	Links                                                                                []Link
	Opened, Closed, Version, SentVersion, MessageID, Ack, Snooze, Reminder, RecoverySent int64
}
type Store struct {
	db    *sql.DB
	mu    sync.Mutex
	limit int
}

func openStore(path string) (*Store, error) {
	db, e := sql.Open("sqlite3", "file:"+path+"?_journal_mode=WAL&_synchronous=FULL&_busy_timeout=5000&_foreign_keys=on")
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(1)
	_, e = db.Exec(`PRAGMA max_page_count=65536;
 CREATE TABLE IF NOT EXISTS signals (key TEXT PRIMARY KEY, grp TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, severity TEXT NOT NULL, state INTEGER NOT NULL, stamp INTEGER NOT NULL, updated INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, grp TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, severity TEXT NOT NULL, opened INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, sent_version INTEGER NOT NULL DEFAULT 0, message_id INTEGER NOT NULL DEFAULT 0, ack INTEGER NOT NULL DEFAULT 0, snooze INTEGER NOT NULL DEFAULT 0, reminder INTEGER NOT NULL, recovery_sent INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
 CREATE UNIQUE INDEX IF NOT EXISTS incident_open ON incidents(grp) WHERE closed=0;
 CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS signal_links (key TEXT PRIMARY KEY REFERENCES signals(key) ON DELETE CASCADE, links TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS incident_links (id INTEGER PRIMARY KEY REFERENCES incidents(id) ON DELETE CASCADE, links TEXT NOT NULL);
 INSERT OR IGNORE INTO meta VALUES ('offset',0);`)
	if e != nil {
		db.Close()
		return nil, e
	}
	s := &Store{db: db, limit: 10000}
	if e = s.backfillLinks(); e != nil {
		db.Close()
		return nil, e
	}
	return s, nil
}

var errFull = errors.New("queue capacity reached")

func (s *Store) accept(events []Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, e := s.db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	var pending int
	if e = tx.QueryRow(`SELECT count(*) FROM incidents WHERE version>sent_version OR (closed>0 AND recovery_sent=0)`).Scan(&pending); e != nil {
		return e
	}
	groups := map[string]bool{}
	for _, v := range events {
		var stamp int64
		var state int
		var oldgroup string
		e = tx.QueryRow(`SELECT stamp,state,grp FROM signals WHERE key=?`, v.Key).Scan(&stamp, &state, &oldgroup)
		if e != nil && !errors.Is(e, sql.ErrNoRows) {
			return e
		}
		if e == nil && (v.Stamp < stamp || (v.Stamp == stamp && v.State != state)) {
			continue
		}
		if e == nil && oldgroup != v.Group {
			return errors.New("signal group changed; migrate explicitly")
		}
		if e == nil && v.State == 2 {
			v.State = state
		} // maintenance/pending cannot prove recovery
		// Unknown first observations do not create incidents.
		if _, e = tx.Exec(`INSERT INTO signals VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,detail=excluded.detail,severity=excluded.severity,state=excluded.state,stamp=excluded.stamp,updated=excluded.updated`, v.Key, v.Group, v.Title, v.Detail, v.Severity, v.State, v.Stamp, now); e != nil {
			return e
		}
		links, _ := json.Marshal(v.Links)
		if _, e = tx.Exec(`INSERT INTO signal_links VALUES (?,?) ON CONFLICT(key) DO UPDATE SET links=excluded.links`, v.Key, string(links)); e != nil {
			return e
		}
		groups[v.Group] = true
	}
	keys := make([]string, 0, len(groups))
	for g := range groups {
		keys = append(keys, g)
	}
	sort.Strings(keys)
	for _, g := range keys {
		rows, e := tx.Query(`SELECT title,detail,severity,COALESCE(links,'null') FROM signals LEFT JOIN signal_links USING(key) WHERE grp=? AND state=1 ORDER BY key`, g)
		if e != nil {
			return e
		}
		var title string
		severity := "warning"
		var parts []string
		var links []Link
		for rows.Next() {
			var t, d, sev, rawLinks string
			if e = rows.Scan(&t, &d, &sev, &rawLinks); e != nil {
				rows.Close()
				return e
			}
			if title == "" {
				title = t
			}
			if sev == "critical" {
				severity = sev
			}
			parts = append(parts, t+": "+d)
			var sourceLinks []Link
			if e = json.Unmarshal([]byte(rawLinks), &sourceLinks); e != nil {
				rows.Close()
				return e
			}
			links = append(links, sourceLinks...)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		detail := clip(strings.Join(parts, "\n"), 1800)
		var id int64
		var olddetail, oldtitle, oldsev, oldlinks string
		encoded, _ := json.Marshal(uniqueLinks(links))
		linkJSON := string(encoded)
		e = tx.QueryRow(`SELECT incidents.id,detail,title,severity,COALESCE(links,'null') FROM incidents LEFT JOIN incident_links USING(id) WHERE grp=? AND closed=0`, g).Scan(&id, &olddetail, &oldtitle, &oldsev, &oldlinks)
		if e != nil && !errors.Is(e, sql.ErrNoRows) {
			return e
		}
		if len(parts) > 0 {
			if errors.Is(e, sql.ErrNoRows) {
				if pending >= s.limit {
					return errFull
				}
				pending++
				result, err := tx.Exec(`INSERT INTO incidents(grp,title,detail,severity,opened,reminder) VALUES(?,?,?,?,?,?)`, g, title, detail, severity, now, now+repeatFor(severity))
				if err != nil {
					return err
				}
				id, e = result.LastInsertId()
				if e != nil {
					return e
				}
			} else if olddetail != detail || oldtitle != title || oldsev != severity || oldlinks != linkJSON {
				if _, e = tx.Exec(`UPDATE incidents SET title=?,detail=?,severity=?,version=version+1 WHERE id=?`, title, detail, severity, id); e != nil {
					return e
				}
			}
			if _, e = tx.Exec(`INSERT INTO incident_links VALUES (?,?) ON CONFLICT(id) DO UPDATE SET links=excluded.links`, id, linkJSON); e != nil {
				return e
			}
		} else if e == nil {
			if _, e = tx.Exec(`UPDATE incidents SET closed=?,version=version+1,snooze=0 WHERE id=?`, now, id); e != nil {
				return e
			}
		}
	}
	var count int
	if e = tx.QueryRow(`SELECT count(*) FROM signals`).Scan(&count); e != nil {
		return e
	}
	if count > 20000 {
		return errFull
	}
	return tx.Commit()
}
func repeatFor(severity string) int64 {
	if severity == "critical" {
		return 4 * 3600
	}
	return 12 * 3600
}

const selectIncident = `SELECT id,grp,title,detail,severity,opened,closed,version,sent_version,message_id,ack,snooze,reminder,recovery_sent,COALESCE((SELECT links FROM incident_links WHERE id=incidents.id),'null') FROM incidents`

func scanIncident(row interface{ Scan(...any) error }) (v Incident, e error) {
	var links string
	e = row.Scan(&v.ID, &v.Group, &v.Title, &v.Detail, &v.Severity, &v.Opened, &v.Closed, &v.Version, &v.SentVersion, &v.MessageID, &v.Ack, &v.Snooze, &v.Reminder, &v.RecoverySent, &links)
	if e == nil {
		e = json.Unmarshal([]byte(links), &v.Links)
	}
	return
}
func (s *Store) next(now int64) (Incident, error) {
	return scanIncident(s.db.QueryRow(selectIncident+` WHERE next_attempt<=? AND (version>sent_version OR (closed>0 AND recovery_sent=0) OR (closed=0 AND ack=0 AND reminder<=? AND snooze<=?)) ORDER BY id LIMIT 1`, now, now, now))
}
func (s *Store) cleanup() {
	s.db.Exec(`DELETE FROM incidents WHERE closed>0 AND closed<unixepoch()-2592000 AND version=sent_version AND recovery_sent=1`)
	s.db.Exec(`DELETE FROM signals WHERE state!=1 AND updated<unixepoch()-7776000`)
	s.db.Exec(`PRAGMA wal_checkpoint(PASSIVE)`)
}
func clip(s string, n int) string {
	r := []rune(strings.TrimSpace(s))
	if len(r) > n {
		return string(r[:n]) + "…"
	}
	return string(r)
}

var urlPattern = regexp.MustCompile(`https?://[^\s]+`)
var credentialPattern = regexp.MustCompile(`(?i)(token|password|secret|authorization)\s*[:=]\s*\S+`)

func clean(s string, n int) string {
	return clip(credentialPattern.ReplaceAllString(urlPattern.ReplaceAllString(s, "[URL]"), "[redacted]"), n)
}
func hash(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:16]) }
func timestamp(s string) (int64, error) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.999999999"} {
		t, e := time.Parse(layout, s)
		if e == nil && !t.IsZero() {
			return t.UnixNano(), nil
		}
	}
	return 0, errors.New("missing/invalid event time")
}

type AMPayload struct {
	Alerts []struct {
		Status, StartsAt, EndsAt string
		Labels, Annotations      map[string]string
	}
}

func parseAM(r io.Reader) ([]Event, error) {
	var p AMPayload
	if e := json.NewDecoder(r).Decode(&p); e != nil {
		return nil, e
	}
	if len(p.Alerts) == 0 || len(p.Alerts) > 200 {
		return nil, errors.New("invalid alert count")
	}
	var out []Event
	for _, a := range p.Alerts {
		state := 1
		ts := a.StartsAt
		if a.Status == "resolved" {
			state = 0
			ts = a.EndsAt
		} else if a.Status != "firing" {
			return nil, errors.New("invalid alert status")
		}
		stamp, e := timestamp(ts)
		if e != nil {
			return nil, e
		}
		name := a.Labels["alertname"]
		if name == "" {
			return nil, errors.New("missing alertname")
		}
		b, _ := json.Marshal(a.Labels)
		key := "am:" + hash(string(b))
		group := a.Labels["notify_service"]
		if group == "" && a.Labels["namespace"] == "updspace-id" {
			group = "updspace-id"
		}
		if group == "" {
			group = key
		}
		sev := a.Labels["severity"]
		if sev != "critical" {
			sev = "warning"
		}
		detail := a.Annotations["summary"]
		if detail == "" {
			detail = name
		}
		description := a.Annotations["description"]
		if description == "" && name == "IDBackupRecoveryPending" {
			description = idBackupExplanation
		}
		if description != "" {
			detail += "\n" + description
		}
		out = append(out, Event{Key: key, Group: clip(group, 100), Title: clean(name, 120), Detail: clean(detail, 900), Severity: sev, State: state, Stamp: stamp, Links: alertLinks(a.Labels)})
	}
	return out, nil
}

type KumaPayload struct {
	Monitor *struct {
		ID   int64  `json:"id"`
		Name string `json:"name"`
	} `json:"monitor"`
	Heartbeat *struct {
		MonitorID int64  `json:"monitorID"`
		Status    *int   `json:"status"`
		Time      string `json:"time"`
		Msg       string `json:"msg"`
		ID        int64  `json:"id"`
	} `json:"heartbeat"`
}

var statusCode = regexp.MustCompile(`(?i)(?:status code|HTTP)[ :]*(\d{3})`)

func parseKuma(r io.Reader) ([]Event, error) {
	var p KumaPayload
	if e := json.NewDecoder(r).Decode(&p); e != nil {
		return nil, e
	}
	if p.Monitor == nil && p.Heartbeat == nil {
		return nil, nil
	} // Kuma's "Test" only checks receiver/auth, not Telegram delivery.
	if p.Monitor == nil || p.Heartbeat == nil || p.Monitor.ID <= 0 || p.Heartbeat.Status == nil {
		return nil, errors.New("missing monitor/heartbeat")
	}
	if p.Heartbeat.MonitorID != 0 && p.Heartbeat.MonitorID != p.Monitor.ID {
		return nil, errors.New("monitor mismatch")
	}
	stamp, e := timestamp(p.Heartbeat.Time)
	if e != nil {
		return nil, e
	}
	state := 2
	detail := "Проверка временно не определяет доступность"
	switch *p.Heartbeat.Status {
	case 0:
		state = 1
		detail = "Проверка недоступна"
		if m := statusCode.FindStringSubmatch(p.Heartbeat.Msg); len(m) == 2 {
			detail = "HTTP " + m[1]
		}
	case 1:
		state = 0
		detail = "Проверка успешна"
	case 2, 3:
	default:
		return nil, errors.New("invalid status")
	}
	key := fmt.Sprintf("kuma:%d", p.Monitor.ID)
	group := key
	if p.Monitor.ID == 16 {
		group = "updspace-id"
	}
	return []Event{{Key: key, Group: group, Title: clean(p.Monitor.Name, 120), Detail: detail, Severity: "critical", State: state, Stamp: stamp, Links: kumaLinks(p.Monitor.ID, p.Monitor.Name)}}, nil
}

type Telegram struct {
	client      *http.Client
	base, token string
	chat        int64
	allowed     map[int64]bool
}
type apiError struct {
	code, retry int
	unchanged   bool
}

func (e *apiError) Error() string { return fmt.Sprintf("telegram error code=%d", e.code) }
func (t *Telegram) call(ctx context.Context, method string, payload any, result any) error {
	b, e := json.Marshal(payload)
	if e != nil {
		return errors.New("encode telegram request")
	}
	req, e := http.NewRequestWithContext(ctx, "POST", t.base+"/bot"+t.token+"/"+method, bytes.NewReader(b))
	if e != nil {
		return errors.New("invalid telegram config")
	}
	req.Header.Set("Content-Type", "application/json")
	res, e := t.client.Do(req)
	if e != nil {
		return errors.New("telegram transport unavailable")
	}
	defer res.Body.Close()
	var env struct {
		OK          bool            `json:"ok"`
		Result      json.RawMessage `json:"result"`
		ErrorCode   int             `json:"error_code"`
		Description string          `json:"description"`
		Parameters  struct {
			RetryAfter int `json:"retry_after"`
		} `json:"parameters"`
	}
	if e = json.NewDecoder(io.LimitReader(res.Body, 2<<20)).Decode(&env); e != nil {
		return errors.New("invalid telegram response")
	}
	if !env.OK || res.StatusCode != 200 {
		code := env.ErrorCode
		if code == 0 {
			code = res.StatusCode
		}
		return &apiError{code: code, retry: env.Parameters.RetryAfter, unchanged: code == 400 && strings.Contains(env.Description, "message is not modified")}
	}
	if result != nil {
		return json.Unmarshal(env.Result, result)
	}
	return nil
}
func telegramClient(socks string) (*http.Client, error) {
	dialer := &net.Dialer{Timeout: 8 * time.Second, KeepAlive: 30 * time.Second}
	d, e := proxy.SOCKS5("tcp", socks, nil, dialer)
	if e != nil {
		return nil, e
	}
	cd, ok := d.(proxy.ContextDialer)
	if !ok {
		return nil, errors.New("SOCKS dialer needs context support")
	}
	tr := &http.Transport{DialContext: cd.DialContext, TLSHandshakeTimeout: 10 * time.Second, ResponseHeaderTimeout: 40 * time.Second, MaxIdleConns: 4, MaxConnsPerHost: 4, IdleConnTimeout: 90 * time.Second}
	return &http.Client{Transport: tr, Timeout: 45 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}, nil
}
func (t *Telegram) card(v Incident) map[string]any {
	status := "🔴 СБОЙ"
	if v.Severity != "critical" {
		status = "🟡 ПРЕДУПРЕЖДЕНИЕ"
	}
	if v.Closed > 0 {
		status = "🟢 ВОССТАНОВЛЕНО"
	}
	loc := time.FixedZone("MSK", 3*3600)
	text := fmt.Sprintf("%s · %s\nИнцидент #%d · %s\n%s\nНачало: %s", status, v.Title, v.ID, v.Severity, v.Detail, time.Unix(v.Opened, 0).In(loc).Format("02.01 15:04:05 MSK"))
	if v.Closed > 0 {
		text += fmt.Sprintf("\nВосстановление: %s · длительность %s", time.Unix(v.Closed, 0).In(loc).Format("02.01 15:04:05 MSK"), time.Duration(v.Closed-v.Opened)*time.Second)
	}
	if v.Ack == 1 {
		text += "\n👀 Принято в работу"
	}
	if v.Snooze > time.Now().Unix() {
		text += "\n🔕 Повтор отложен до " + time.Unix(v.Snooze, 0).In(loc).Format("15:04 MSK")
	}
	if v.MessageID == 0 && time.Now().Unix()-v.Opened > 120 {
		text += "\nДоставка задержалась; состояние актуально на момент отправки."
	}
	var buttons [][]map[string]string
	for _, link := range uniqueLinks(v.Links) {
		buttons = append(buttons, []map[string]string{{"text": link.Text, "url": link.URL}})
	}
	buttons = append(buttons, []map[string]string{{"text": "Общий статус", "url": "https://status.updspace.com"}, {"text": "Grafana", "url": "https://grafana.updspace.com"}})
	if v.Closed == 0 {
		buttons = append(buttons, []map[string]string{{"text": "Принял", "callback_data": fmt.Sprintf("ack:%d", v.ID)}, {"text": "Тише 15 мин", "callback_data": fmt.Sprintf("snooze:%d", v.ID)}})
	}
	return map[string]any{"chat_id": t.chat, "text": text, "reply_markup": map[string]any{"inline_keyboard": buttons}, "link_preview_options": map[string]bool{"is_disabled": true}}
}

type App struct {
	store          *Store
	tg             *Telegram
	amKey, kumaKey string
	errors         atomic.Int64
	lastSuccess    atomic.Int64
	lastPoll       atomic.Int64
	accepted       atomic.Int64
}

func (a *App) deliver(ctx context.Context, v Incident) error {
	p := a.tg.card(v)
	var response struct {
		MessageID int64 `json:"message_id"`
	}
	if v.Version > v.SentVersion {
		method := "sendMessage"
		if v.MessageID != 0 {
			method = "editMessageText"
			p["message_id"] = v.MessageID
		}
		e := a.tg.call(ctx, method, p, &response)
		var ae *apiError
		if e != nil && !(method == "editMessageText" && errors.As(e, &ae) && ae.unchanged) {
			return e
		}
		if v.MessageID == 0 {
			if response.MessageID == 0 {
				return errors.New("telegram returned no message id")
			}
			v.MessageID = response.MessageID
			if v.Closed == 0 {
				oldReminder := v.Reminder
				v.Reminder = time.Now().Unix() + repeatFor(v.Severity)
				if _, err := a.store.db.Exec(`UPDATE incidents SET reminder=? WHERE id=? AND reminder=?`, v.Reminder, v.ID, oldReminder); err != nil {
					return err
				}
			}

			if v.Closed > 0 {
				v.RecoverySent = 1
			}
		}
		_, e = a.store.db.Exec(`UPDATE incidents SET message_id=?,sent_version=?,recovery_sent=? WHERE id=?`, v.MessageID, v.Version, v.RecoverySent, v.ID)
		if e != nil {
			return e
		}
	}
	if v.Closed > 0 && v.RecoverySent == 0 {
		p = map[string]any{"chat_id": a.tg.chat, "text": fmt.Sprintf("🟢 Восстановлено: %s\nИнцидент #%d · %s", v.Title, v.ID, time.Duration(v.Closed-v.Opened)*time.Second), "reply_parameters": map[string]any{"message_id": v.MessageID, "allow_sending_without_reply": true}}
		p["reply_markup"] = a.tg.card(v)["reply_markup"]
		if e := a.tg.call(ctx, "sendMessage", p, nil); e != nil {
			return e
		}
		if _, e := a.store.db.Exec(`UPDATE incidents SET recovery_sent=1 WHERE id=?`, v.ID); e != nil {
			return e
		}
	}
	if v.Closed == 0 && v.Ack == 0 && v.Reminder <= time.Now().Unix() && v.Snooze <= time.Now().Unix() {
		// Re-read actions immediately before a reminder: callbacks may arrive during card I/O.
		fresh, e := scanIncident(a.store.db.QueryRow(selectIncident+` WHERE id=?`, v.ID))
		if e != nil {
			return e
		}
		if fresh.Closed == 0 && fresh.Ack == 0 && fresh.Snooze <= time.Now().Unix() && fresh.Reminder <= time.Now().Unix() {
			p = a.tg.card(fresh)
			p["text"] = "Напоминание\n" + p["text"].(string)
			p["reply_parameters"] = map[string]any{"message_id": v.MessageID, "allow_sending_without_reply": true}
			if e := a.tg.call(ctx, "sendMessage", p, nil); e != nil {
				return e
			}
			if _, e = a.store.db.Exec(`UPDATE incidents SET reminder=? WHERE id=? AND reminder=?`, time.Now().Unix()+repeatFor(fresh.Severity), v.ID, fresh.Reminder); e != nil {
				return e
			}
		}
	}
	_, e := a.store.db.Exec(`UPDATE incidents SET attempts=0,next_attempt=0 WHERE id=?`, v.ID)
	return e
}
func (a *App) worker(ctx context.Context) {
	tick := time.NewTicker(1100 * time.Millisecond)
	defer tick.Stop()
	var blockedUntil int64
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		if a.tg == nil || time.Now().Unix() < blockedUntil {
			continue
		}
		v, e := a.store.next(time.Now().Unix())
		if errors.Is(e, sql.ErrNoRows) {
			continue
		}
		if e != nil {
			a.errors.Add(1)
			continue
		}
		e = a.deliver(ctx, v)
		if e == nil {
			a.lastSuccess.Store(time.Now().Unix())
			continue
		}
		a.errors.Add(1)
		var attempts int
		_ = a.store.db.QueryRow(`SELECT attempts FROM incidents WHERE id=?`, v.ID).Scan(&attempts)
		delay := int64(5) << min(attempts, 6)
		var ae *apiError
		if errors.As(e, &ae) {
			if ae.retry > 0 {
				delay = max(delay, int64(ae.retry))
			}
			if ae.code == 401 || ae.code == 403 {
				delay = 300
			}
			if ae.code == 429 {
				blockedUntil = time.Now().Unix() + delay
			}
		}
		_, _ = a.store.db.Exec(`UPDATE incidents SET attempts=attempts+1,next_attempt=? WHERE id=?`, time.Now().Unix()+delay, v.ID)
		log.Printf("delivery deferred incident=%d retry_seconds=%d", v.ID, delay) // Never log HTTP errors: URLs contain the bot token.
	}
}

type Update struct {
	ID       int64 `json:"update_id"`
	Callback *struct {
		ID, Data string
		From     struct{ ID int64 }
		Message  struct {
			MessageID int64 `json:"message_id"`
			Chat      struct{ ID int64 }
		}
	} `json:"callback_query"`
}

func (a *App) processUpdate(u Update) error {
	a.store.mu.Lock()
	defer a.store.mu.Unlock()
	tx, e := a.store.db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	var offset int64
	if e = tx.QueryRow(`SELECT value FROM meta WHERE key='offset'`).Scan(&offset); e != nil {
		return e
	}
	if u.ID < offset {
		return nil
	}
	c := u.Callback
	if c != nil && c.Message.Chat.ID == a.tg.chat && a.tg.allowed[c.From.ID] {
		parts := strings.Split(c.Data, ":")
		if len(parts) == 2 {
			id, err := strconv.ParseInt(parts[1], 10, 64)
			if err == nil {
				switch parts[0] {
				case "ack":
					_, e = tx.Exec(`UPDATE incidents SET ack=1,version=version+1 WHERE id=? AND message_id=? AND closed=0 AND ack=0`, id, c.Message.MessageID)
				case "snooze":
					_, e = tx.Exec(`UPDATE incidents SET ack=0,snooze=unixepoch()+900,reminder=unixepoch()+900,version=version+1 WHERE id=? AND message_id=? AND closed=0`, id, c.Message.MessageID)
				}
				if e != nil {
					return e
				}
			}
		}
	}
	if _, e = tx.Exec(`UPDATE meta SET value=? WHERE key='offset'`, u.ID+1); e != nil {
		return e
	}
	return tx.Commit()
}
func (a *App) poll(ctx context.Context) {
	for ctx.Err() == nil {
		if a.tg == nil {
			return
		}
		var offset int64
		if e := a.store.db.QueryRow(`SELECT value FROM meta WHERE key='offset'`).Scan(&offset); e != nil {
			if !pause(ctx, 5*time.Second) {
				return
			}
			continue
		}
		var updates []Update
		e := a.tg.call(ctx, "getUpdates", map[string]any{"offset": offset, "timeout": 25, "allowed_updates": []string{"callback_query"}}, &updates)
		if e != nil {
			a.errors.Add(1)
			if !pause(ctx, 10*time.Second) {
				return
			}
			continue
		}
		a.lastPoll.Store(time.Now().Unix())
		failed := false
		for _, u := range updates {
			if u.ID < offset {
				continue
			}
			if e = a.processUpdate(u); e != nil {
				failed = true
				break
			}
			// Action and offset were committed in one transaction.
			offset = u.ID + 1
			if u.Callback != nil {
				text := "Команда недоступна"
				if u.Callback.Message.Chat.ID == a.tg.chat && a.tg.allowed[u.Callback.From.ID] {
					text = "Принято"
				}
				_ = a.tg.call(ctx, "answerCallbackQuery", map[string]any{"callback_query_id": u.Callback.ID, "text": text}, nil)
			}
		}
		if failed && !pause(ctx, 5*time.Second) {
			return
		}
	}
}
func pause(ctx context.Context, d time.Duration) bool {
	select {
	case <-ctx.Done():
		return false
	case <-time.After(d):
		return true
	}
}
func (a *App) handler() http.Handler {
	mux := http.NewServeMux()
	for _, s := range []struct {
		path, key string
		parse     func(io.Reader) ([]Event, error)
	}{{"/webhooks/alertmanager", a.amKey, parseAM}, {"/webhooks/kuma", a.kumaKey, parseKuma}} {
		mux.HandleFunc("POST "+s.path, func(w http.ResponseWriter, r *http.Request) {
			given := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if len(s.key) < 32 || subtle.ConstantTimeCompare([]byte(given), []byte(s.key)) != 1 {
				http.Error(w, "unauthorized", 401)
				return
			}
			events, e := s.parse(http.MaxBytesReader(w, r.Body, 256<<10))
			if e != nil {
				http.Error(w, "invalid payload", 400)
				return
			}
			if e = a.store.accept(events); e != nil {
				http.Error(w, "storage unavailable", 503)
				return
			}
			a.accepted.Add(int64(len(events)))
			w.WriteHeader(202)
		})
	}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		// A writable transaction proves local acceptance capability; Telegram is intentionally independent.
		if _, e := a.store.db.ExecContext(ctx, `INSERT INTO meta VALUES ('readiness',unixepoch()) ON CONFLICT(key) DO UPDATE SET value=excluded.value`); e != nil {
			http.Error(w, "storage unavailable", 503)
			return
		}
		w.WriteHeader(200)
	})
	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, r *http.Request) {
		var pending, oldest, active int64
		e := a.store.db.QueryRow(`SELECT count(*),coalesce(min(opened),0) FROM incidents WHERE version>sent_version OR (closed>0 AND recovery_sent=0)`).Scan(&pending, &oldest)
		if e != nil {
			http.Error(w, "storage unavailable", 503)
			return
		}
		if e = a.store.db.QueryRow(`SELECT count(*) FROM incidents WHERE closed=0`).Scan(&active); e != nil {
			http.Error(w, "storage unavailable", 503)
			return
		}
		age := int64(0)
		if oldest > 0 {
			age = time.Now().Unix() - oldest
		}
		enabled := 0
		if a.tg != nil {
			enabled = 1
		}
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		var host map[string]int64
		if path := os.Getenv("HOST_STATUS_PATH"); path != "" {
			if data, err := os.ReadFile(path); err == nil {
				_ = json.Unmarshal(data, &host)
			}
		}
		for _, name := range []string{"host_status_timestamp_seconds", "vpn_last_refresh_timestamp_seconds", "vpn_last_probe_timestamp_seconds", "kuma_reconcile_last_success_timestamp_seconds", "id_backup_resume_pending"} {
			fmt.Fprintf(w, "notify_bridge_%s %d\n", name, host[name])
		}

		fmt.Fprintf(w, "notify_bridge_sender_enabled %d\nnotify_bridge_pending %d\nnotify_bridge_oldest_pending_seconds %d\nnotify_bridge_active_incidents %d\nnotify_bridge_delivery_errors_total %d\nnotify_bridge_last_delivery_timestamp_seconds %d\nnotify_bridge_last_poll_timestamp_seconds %d\nnotify_bridge_accepted_events_total %d\n", enabled, pending, age, active, a.errors.Load(), a.lastSuccess.Load(), a.lastPoll.Load(), a.accepted.Load())
	})
	return mux
}
func readSecret(name string) string {
	path := os.Getenv(name + "_FILE")
	if path == "" {
		return ""
	}
	b, e := os.ReadFile(path)
	if e != nil {
		log.Fatalf("cannot read %s_FILE", name)
	}
	return strings.TrimSpace(string(b))
}
func main() {
	if len(os.Args) > 1 && os.Args[1] == "--egress" {
		serveEgress()
		return
	}
	log.SetFlags(log.LstdFlags | log.LUTC)
	path := os.Getenv("DB_PATH")
	if path == "" {
		path = "/data/bridge.db"
	}
	s, e := openStore(path)
	if e != nil {
		log.Fatal("cannot open database")
	}
	defer s.db.Close()
	a := &App{store: s, amKey: readSecret("ALERTMANAGER_KEY"), kumaKey: readSecret("KUMA_KEY")}
	if len(a.amKey) < 32 || len(a.kumaKey) < 32 {
		log.Fatal("webhook keys are required")
	}
	token := readSecret("TELEGRAM_TOKEN")
	if token != "" {
		chat, e := strconv.ParseInt(readSecret("TELEGRAM_CHAT"), 10, 64)
		if e != nil || chat == 0 {
			log.Fatal("invalid chat id")
		}
		allowed := map[int64]bool{}
		for _, s := range strings.Split(readSecret("TELEGRAM_USERS"), ",") {
			id, e := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
			if e != nil || id <= 0 {
				log.Fatal("invalid allowed user id")
			}
			allowed[id] = true
		}
		client, e := telegramClient(os.Getenv("SOCKS_PROXY"))
		if e != nil {
			log.Fatal("invalid SOCKS config")
		}
		a.tg = &Telegram{client: client, base: "https://api.telegram.org", token: token, chat: chat, allowed: allowed}
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go a.worker(ctx)
	go a.poll(ctx)
	go func() {
		for pause(ctx, time.Hour) {
			s.cleanup()
		}
	}()
	srv := &http.Server{Addr: ":8080", Handler: a.handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(shutdown)
	}()
	log.Printf("notify-bridge started sender_enabled=%t", a.tg != nil)
	if e = srv.ListenAndServe(); e != nil && !errors.Is(e, http.ErrServerClosed) {
		log.Fatal("http server failed")
	}
}
