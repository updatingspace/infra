package main

import (
	"encoding/json"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
)

func TestAlertContextAndSafeDestinations(t *testing.T) {
	legacy, err := parseAM(strings.NewReader(`{"alerts":[{"status":"firing","startsAt":"2026-10-21T00:00:00Z","labels":{"alertname":"IDBackupRecoveryPending","severity":"critical"}}]}`))
	if err != nil || !strings.Contains(legacy[0].Detail, "ID может работать") || legacy[0].Severity != "critical" {
		t.Fatal("backup context must explain the marker without overriding the source severity", err)
	}
	events, err := parseAM(strings.NewReader(`{"alerts":[{"status":"firing","startsAt":"2026-10-21T00:00:00Z","labels":{"alertname":"IDBackupRecoveryPending","notify_service":"id-backup","instance":"bridge:8080","severity":"warning","password":"SENSITIVE"},"annotations":{"summary":"Остался маркер","description":"ID может работать. token=SENSITIVE"},"generatorURL":"https://evil.test/?token=SENSITIVE"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	v := events[0]
	if len(v.Links) != 2 || v.Links[1].URL != "https://id.updspace.com/" || !strings.Contains(v.Detail, "ID может работать") {
		t.Fatalf("missing context: %+v", v)
	}
	u, err := url.Parse(v.Links[0].URL)
	if err != nil {
		t.Fatal(err)
	}
	q, _ := url.ParseQuery(strings.SplitN(u.Fragment, "?", 2)[1])
	if q.Get("filter") != `{alertname="IDBackupRecoveryPending",notify_service="id-backup",instance="bridge:8080",severity="warning"}` {
		t.Fatal(q)
	}
	b, _ := json.Marshal(v)
	if strings.Contains(string(b), "SENSITIVE") || strings.Contains(string(b), "evil.test") {
		t.Fatal("sensitive context escaped")
	}
	card := (&Telegram{}).card(Incident{Title: v.Title, Detail: v.Detail, Severity: v.Severity, Links: v.Links})
	if !strings.HasPrefix(card["text"].(string), "🟡 ПРЕДУПРЕЖДЕНИЕ") {
		t.Fatal(card)
	}
	rows := card["reply_markup"].(map[string]any)["inline_keyboard"].([][]map[string]string)
	if rows[0][0]["url"] != v.Links[0].URL || rows[1][0]["url"] != v.Links[1].URL {
		t.Fatal("context buttons are not first")
	}
}

func TestKumaLinksIgnoreRawCredentialsAndUnknownSites(t *testing.T) {
	events, err := parseKuma(strings.NewReader(`{"monitor":{"id":16,"name":"ID","url":"https://evil.test/SENSITIVE?token=SENSITIVE"},"heartbeat":{"status":0,"time":"2026-10-21T00:00:00Z"}}`))
	if err != nil {
		t.Fatal(err)
	}
	links := events[0].Links
	if len(links) != 2 || links[0].URL != "https://status.updspace.com/dashboard/16" || links[1].URL != "https://id.updspace.com/" {
		t.Fatal(links)
	}
	if len(kumaLinks(9999, "Unknown")) != 1 {
		t.Fatal("invented a public site")
	}
}

func TestLinksSurviveRestartAndRecoveryAndRefreshSameEvent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "queue.db")
	s, err := openStore(path)
	if err != nil {
		t.Fatal(err)
	}
	e := event("am:one", 1, 10)
	e.Links = alertLinks(map[string]string{"alertname": "IDDown", "notify_service": "updspace-id"})
	accept(t, s, e)
	v := incident(t, s)
	e.Links[0].URL += "&silenced=true"
	accept(t, s, e) // AM repeats startsAt: new metadata must still update the card.
	if incident(t, s).Version != v.Version+1 {
		t.Fatal("same-time context was ignored")
	}
	accept(t, s, e)
	if incident(t, s).Version != v.Version+1 {
		t.Fatal("identical context caused churn")
	}
	e.State = 0
	e.Stamp = 20
	e.Links = nil
	accept(t, s, e)
	s.db.Close()
	s, err = openStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	got := incident(t, s)
	if got.Closed == 0 || len(got.Links) != 2 || !strings.HasSuffix(got.Links[0].URL, "silenced=true") {
		t.Fatal("recovery lost original links", got)
	}
}

func TestExistingAcknowledgedCardGainsLinksWithoutNewIncident(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.db")
	s, err := openStore(path)
	if err != nil {
		t.Fatal(err)
	}
	e := event("kuma:16", 1, 10)
	accept(t, s, e)
	v := incident(t, s)
	if _, err = s.db.Exec(`DELETE FROM signal_links; DELETE FROM incident_links; UPDATE incidents SET ack=1,message_id=99,sent_version=version`); err != nil {
		t.Fatal(err)
	}
	s.db.Close()
	s, err = openStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	got := incident(t, s)
	if got.ID != v.ID || got.Ack != 1 || got.MessageID != 99 || got.Version != v.Version+1 || len(got.Links) != 2 {
		t.Fatal("legacy card was not preserved", got)
	}
	accept(t, s, event("kuma:16", 0, 9))
	if incident(t, s).Closed != 0 {
		t.Fatal("old event replaced current context")
	}
}

func TestLegacyAlertDoesNotInventLabelMatchers(t *testing.T) {
	s := testStore(t)
	e := event("am:legacy", 1, 10)
	e.Group = "updspace-id" // Inferred from namespace; notify_service need not exist.
	e.Title = "IDDown"
	accept(t, s, e)
	if _, err := s.db.Exec(`DELETE FROM signal_links; DELETE FROM incident_links`); err != nil {
		t.Fatal(err)
	}
	if err := s.backfillLinks(); err != nil {
		t.Fatal(err)
	}
	got := incident(t, s)
	u, _ := url.Parse(got.Links[0].URL)
	q, _ := url.ParseQuery(strings.SplitN(u.Fragment, "?", 2)[1])
	if q.Get("filter") != `{alertname="IDDown"}` || len(got.Links) != 2 {
		t.Fatal(got.Links)
	}
}
