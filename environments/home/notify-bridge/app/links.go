package main

import (
	"fmt"
	"net/url"
	"strconv"
	"strings"
)

type Link struct {
	Text string `json:"text"`
	URL  string `json:"url"`
}

const idBackupExplanation = "После backup остался маркер незавершённого восстановления. Это не проверка доступности сайта: ID может работать. Проверьте журнал backup и завершение восстановления; не удаляйте маркер без проверки."

// Public destinations are reviewed configuration, never raw monitor URLs (which
// may contain passwords, signed paths, query tokens, or private host addresses).
var monitorSites = map[int64]string{
	1: "https://cloud.joutak.ru/", 2: "https://is-schedule.updspace.com/",
	3: "https://joutak.ru/", 4: "https://spbetu.ru/", 5: "https://ttnr.ru/",
	6: "https://updspace.com/", 7: "https://wiki.joutak.ru/",
	8: "https://xn--80abjdnrwhz3i.xn--p1ai/",
	9: "https://grafana.updspace.com/", 10: "https://prometheus.updspace.com/",
	11: "https://alerts.updspace.com/", 13: "https://pz-admin.updspace.com/",
	16: "https://id.updspace.com/", 17: "https://portal.updspace.com/",
}

func kumaLinks(id int64, name string) []Link {
	links := []Link{{"Проверка · " + clean(name, 38), fmt.Sprintf("https://status.updspace.com/dashboard/%d", id)}}
	site := monitorSites[id]
	if id >= 18 && id <= 25 { // Internal Portal service checks share the public application.
		site = "https://portal.updspace.com/"
	}
	if site != "" {
		links = append(links, Link{"Открыть сервис", site})
	}
	return links
}

func alertLinks(labels map[string]string) []Link {
	// Match the concrete alert instance without forwarding arbitrary label data.
	// Unknown labels (including credentials) stay out of Telegram and the URL.
	var matchers []string
	for _, key := range []string{"alertname", "cluster", "namespace", "notify_service", "component", "job", "instance", "pod", "deployment", "container", "node", "severity"} {
		value := labels[key]
		if value != "" && len(value) <= 256 && clean(value, 256) == value {
			matchers = append(matchers, key+"="+strconv.Quote(value))
		}
	}
	filter := "{" + strings.Join(matchers, ",") + "}"
	links := []Link{{"Алерт · " + clean(labels["alertname"], 38), "https://alerts.updspace.com/#/alerts?active=true&silenced=true&inhibited=true&muted=true&filter=" + url.QueryEscape(filter)}}
	service := labels["notify_service"]
	if service == "" {
		service = labels["namespace"]
	}
	site := map[string]string{
		"updspace-id": "https://id.updspace.com/", "id-backup": "https://id.updspace.com/",
		"updspace-portal": "https://portal.updspace.com/", "portal": "https://portal.updspace.com/",
		"zomboid": "https://pz-admin.updspace.com/",
	}[service]
	if site != "" {
		links = append(links, Link{"Открыть сервис", site})
	}
	return links
}

func uniqueLinks(links []Link) []Link {
	var out []Link
	seen := map[string]bool{}
	for _, link := range links {
		if !seen[link.URL] && link.URL != "" {
			out = append(out, link)
			seen[link.URL] = true
		}
		if len(out) == 20 {
			break
		}
	}
	return out
}

// Add context to already-open cards without recreating incidents or discarding
// acknowledgements. Side tables keep rollback to the old binary compatible.
func (s *Store) backfillLinks() error {
	rows, err := s.db.Query(`SELECT key,grp,title,detail,severity,state,stamp FROM signals WHERE state=1 AND NOT EXISTS (SELECT 1 FROM signal_links WHERE signal_links.key=signals.key)`)
	if err != nil {
		return err
	}
	var events []Event
	for rows.Next() {
		var v Event
		if err = rows.Scan(&v.Key, &v.Group, &v.Title, &v.Detail, &v.Severity, &v.State, &v.Stamp); err != nil {
			rows.Close()
			return err
		}
		if strings.HasPrefix(v.Key, "am:") {
			// Legacy records did not retain instance labels; link to the named rule.
			v.Links = alertLinks(map[string]string{"alertname": v.Title})
			context := alertLinks(map[string]string{"alertname": v.Title, "notify_service": v.Group})
			v.Links = append(v.Links, context[1:]...)
			if v.Title == "IDBackupRecoveryPending" {
				v.Detail = clean(v.Detail+"\n"+idBackupExplanation, 900)
			}
		} else if strings.HasPrefix(v.Key, "kuma:") {
			id, e := strconv.ParseInt(strings.TrimPrefix(v.Key, "kuma:"), 10, 64)
			if e == nil {
				v.Links = kumaLinks(id, v.Title)
			}
		}
		events = append(events, v)
	}
	err = rows.Err()
	rows.Close()
	if err != nil || len(events) == 0 {
		return err
	}
	return s.accept(events)
}
