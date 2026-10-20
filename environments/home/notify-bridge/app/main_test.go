package main

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testStore(t *testing.T) *Store {
	t.Helper()
	s, e := openStore(filepath.Join(t.TempDir(), "test.db"))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.db.Close() })
	return s
}
func event(key string, state int, stamp int64) Event {
	return Event{Key: key, Group: "id", Title: "ID", Detail: "HTTP 502", Severity: "critical", State: state, Stamp: stamp}
}
func incident(t *testing.T, s *Store) Incident {
	t.Helper()
	v, e := scanIncident(s.db.QueryRow(selectIncident + ` ORDER BY id DESC LIMIT 1`))
	if e != nil {
		t.Fatal(e)
	}
	return v
}
func accept(t *testing.T, s *Store, e ...Event) {
	t.Helper()
	if err := s.accept(e); err != nil {
		t.Fatal(err)
	}
}
func TestMixedSourcesDuplicateAndOutOfOrder(t *testing.T) {
	s := testStore(t)
	accept(t, s, event("kuma", 1, 10), event("am", 1, 10))
	v := incident(t, s)
	accept(t, s, event("kuma", 1, 10))
	if got := incident(t, s); got.Version != v.Version {
		t.Fatal("duplicate changed incident")
	}
	accept(t, s, event("am", 0, 20))
	if incident(t, s).Closed != 0 {
		t.Fatal("premature recovery")
	}
	accept(t, s, event("kuma", 2, 25))
	if incident(t, s).Closed != 0 {
		t.Fatal("pending closed incident")
	}
	accept(t, s, event("kuma", 0, 30))
	if incident(t, s).Closed == 0 {
		t.Fatal("did not close")
	}
	accept(t, s, event("am", 1, 10))
	if incident(t, s).ID != v.ID {
		t.Fatal("stale firing reopened incident")
	}
	accept(t, s, event("kuma", 1, 40))
	if incident(t, s).ID == v.ID {
		t.Fatal("new outage reused old episode")
	}
}
func TestPersistentQueueAndCapacity(t *testing.T) {
	p := filepath.Join(t.TempDir(), "test.db")
	s, e := openStore(p)
	if e != nil {
		t.Fatal(e)
	}
	s.limit = 1
	accept(t, s, event("x", 1, 1))
	v := event("y", 1, 2)
	v.Group = "other"
	if e = s.accept([]Event{v}); e != errFull {
		t.Fatalf("want full: %v", e)
	}
	var n int
	s.db.QueryRow(`SELECT count(*) FROM signals`).Scan(&n)
	if n != 1 {
		t.Fatal("partial commit on rejected webhook")
	}
	s.db.Close()
	s, e = openStore(p)
	if e != nil {
		t.Fatal(e)
	}
	defer s.db.Close()
	if _, e = s.next(time.Now().Unix()); e != nil {
		t.Fatal("queue did not survive restart", e)
	}
}
func TestReceiverAuthenticationAndNoSensitiveStorage(t *testing.T) {
	s := testStore(t)
	key := strings.Repeat("a", 32)
	a := &App{store: s, kumaKey: key}
	h := a.handler()
	body := `{"monitor":{"id":16,"name":"ID","password":"SENSITIVE","url":"https://x/?token=SENSITIVE"},"heartbeat":{"status":0,"time":"2026-10-21 00:00:00.123","msg":"Request failed with status code 502 password=SENSITIVE"}}`
	for _, auth := range []string{"", "wrong", key} {
		r := httptest.NewRequest("POST", "/webhooks/kuma", strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+auth)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		want := 401
		if auth == key {
			want = 202
		}
		if w.Code != want {
			t.Fatal(w.Code, want)
		}
	}
	v := incident(t, s)
	if strings.Contains(v.Detail, "SENSITIVE") || !strings.Contains(v.Detail, "502") {
		t.Fatal(v.Detail)
	}
	for _, path := range []string{"/readyz", "/healthz", "/metrics"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 {
			t.Fatal(path, w.Code)
		}
	}
}
func TestTelegramRetryAfterAndNoTokenLeak(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(429)
		w.Write([]byte(`{"ok":false,"error_code":429,"description":"sensitive","parameters":{"retry_after":37}}`))
	}))
	defer srv.Close()
	tg := &Telegram{client: srv.Client(), base: srv.URL, token: "TOKEN"}
	e := tg.call(context.Background(), "sendMessage", nil, nil)
	ae, ok := e.(*apiError)
	if !ok || ae.retry != 37 || strings.Contains(e.Error(), "TOKEN") || strings.Contains(e.Error(), "sensitive") {
		t.Fatal(e)
	}
	srv.Close()
	e = tg.call(context.Background(), "sendMessage", nil, nil)
	if strings.Contains(e.Error(), "TOKEN") {
		t.Fatal(e)
	}
}
func TestDeliveryEditRecoveryAndOfflineSummary(t *testing.T) {
	var methods []string
	var texts []string
	id := 100
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.URL.Path)
		var p map[string]any
		json.NewDecoder(r.Body).Decode(&p)
		texts = append(texts, p["text"].(string))
		id++
		json.NewEncoder(w).Encode(map[string]any{"ok": true, "result": map[string]any{"message_id": id}})
	}))
	defer srv.Close()
	s := testStore(t)
	a := &App{store: s, tg: &Telegram{client: srv.Client(), base: srv.URL, token: "T", chat: 1}}
	accept(t, s, event("x", 1, 1))
	v := incident(t, s)
	if e := a.deliver(context.Background(), v); e != nil {
		t.Fatal(e)
	}
	accept(t, s, event("x", 0, 2))
	if e := a.deliver(context.Background(), incident(t, s)); e != nil {
		t.Fatal(e)
	}
	if len(methods) != 3 || methods[0] != "/botT/sendMessage" || methods[1] != "/botT/editMessageText" || methods[2] != "/botT/sendMessage" {
		t.Fatal(methods)
	}
	accept(t, s, event("x", 1, 3))
	accept(t, s, event("x", 0, 4))
	if e := a.deliver(context.Background(), incident(t, s)); e != nil {
		t.Fatal(e)
	}
	if len(methods) != 4 || !strings.Contains(texts[3], "ВОССТАНОВЛЕНО") {
		t.Fatal(methods, texts)
	}
}
func TestCallbackAuthorizationAndAtomicReplay(t *testing.T) {
	s := testStore(t)
	accept(t, s, event("x", 1, 1))
	v := incident(t, s)
	s.db.Exec(`UPDATE incidents SET message_id=99 WHERE id=?`, v.ID)
	a := &App{store: s, tg: &Telegram{chat: 7, allowed: map[int64]bool{8: true}}}
	parse := func(raw string) Update {
		var u Update
		if e := json.Unmarshal([]byte(raw), &u); e != nil {
			t.Fatal(e)
		}
		return u
	}
	bad := parse(`{"update_id":10,"callback_query":{"data":"ack:1","from":{"id":9},"message":{"message_id":99,"chat":{"id":7}}}}`)
	if e := a.processUpdate(bad); e != nil {
		t.Fatal(e)
	}
	if incident(t, s).Ack != 0 {
		t.Fatal("unauthorized action")
	}
	good := parse(`{"update_id":11,"callback_query":{"data":"snooze:1","from":{"id":8},"message":{"message_id":99,"chat":{"id":7}}}}`)
	if e := a.processUpdate(good); e != nil {
		t.Fatal(e)
	}
	v = incident(t, s)
	if v.Snooze == 0 {
		t.Fatal("no snooze")
	}
	if e := a.processUpdate(good); e != nil {
		t.Fatal(e)
	}
	if incident(t, s).Version != v.Version {
		t.Fatal("replayed callback")
	}
	invalid := parse(`{"update_id":12,"callback_query":{"data":"invalid:1","from":{"id":8},"message":{"message_id":99,"chat":{"id":7}}}}`)
	if e := a.processUpdate(invalid); e != nil {
		t.Fatal("poisoned updates", e)
	}
}
func TestAlertmanagerResolveTimestampAndSafeLabels(t *testing.T) {
	ev, e := parseAM(strings.NewReader(`{"alerts":[{"status":"resolved","startsAt":"2026-10-21T00:00:00Z","endsAt":"2026-10-21T01:00:00Z","labels":{"alertname":"IDDown","namespace":"updspace-id","severity":"critical","password":"SENSITIVE"},"annotations":{"summary":"Down https://x/?token=SECRET"}}]}`))
	if e != nil {
		t.Fatal(e)
	}
	if ev[0].Group != "updspace-id" || ev[0].State != 0 || strings.Contains(ev[0].Detail, "SECRET") {
		t.Fatal(ev)
	}
	if _, e = parseAM(strings.NewReader(`{"alerts":[{"status":"firing","labels":{"alertname":"A"}}]}`)); e == nil {
		t.Fatal("accepted missing timestamp")
	}
}

func TestSOCKSRejectsOtherHosts(t *testing.T) {
	for _, host := range []string{"example.com", "127.0.0.1", "api.telegram.org.evil"} {
		server, client := net.Pipe()
		done := make(chan struct{})
		go func() { defer close(done); defer server.Close(); handleSOCKS(server, nil) }()
		client.SetDeadline(time.Now().Add(time.Second))
		client.Write([]byte{5, 1, 0})
		reply := make([]byte, 2)
		io.ReadFull(client, reply)
		req := append([]byte{5, 1, 0, 3, byte(len(host))}, []byte(host)...)
		req = append(req, 1, 187)
		client.Write(req)
		resp := make([]byte, 10)
		if _, e := io.ReadFull(client, resp); e != nil || resp[1] != 2 {
			t.Fatal(host, e, resp)
		}
		client.Close()
		<-done
	}
}

func TestDelayedFirstDeliveryDoesNotImmediatelyRepeat(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Write([]byte(`{"ok":true,"result":{"message_id":77}}`))
	}))
	defer srv.Close()
	s := testStore(t)
	accept(t, s, event("x", 1, 1))
	s.db.Exec(`UPDATE incidents SET reminder=1`)
	a := &App{store: s, tg: &Telegram{client: srv.Client(), base: srv.URL, token: "T", chat: 1}}
	if e := a.deliver(context.Background(), incident(t, s)); e != nil {
		t.Fatal(e)
	}
	if calls != 1 {
		t.Fatalf("initial delivery generated %d messages", calls)
	}
	if incident(t, s).Reminder <= time.Now().Unix() {
		t.Fatal("reminder not rescheduled")
	}
}
