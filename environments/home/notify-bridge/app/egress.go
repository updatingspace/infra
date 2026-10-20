package main

import (
	"context"
	"encoding/binary"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"time"

	"golang.org/x/net/proxy"
)

// This port is intentionally not a general-purpose SOCKS service. Only the
// HTTPS Bot API hostname is permitted; TLS remains end-to-end in the client.
func serveEgress() {
	upstream := os.Getenv("UPSTREAM_SOCKS")
	if upstream == "" {
		upstream = "127.0.0.1:1081"
	}
	var auth *proxy.Auth
	if user := readSecret("UPSTREAM_USER"); user != "" {
		auth = &proxy.Auth{User: user, Password: readSecret("UPSTREAM_PASSWORD")}
	}
	dialer, e := proxy.SOCKS5("tcp", upstream, auth, &net.Dialer{Timeout: 8 * time.Second, KeepAlive: 30 * time.Second})
	if e != nil {
		log.Fatal("invalid upstream config")
	}
	listener, e := net.Listen("tcp", ":1080")
	if e != nil {
		log.Fatal("cannot bind egress")
	}
	defer listener.Close()
	limit := make(chan struct{}, 16)
	for {
		c, e := listener.Accept()
		if e != nil {
			return
		}
		select {
		case limit <- struct{}{}:
			go func() { defer func() { <-limit }(); defer c.Close(); handleSOCKS(c, dialer) }()
		default:
			c.Close()
		}
	}
}
func handleSOCKS(c net.Conn, d proxy.Dialer) {
	c.SetDeadline(time.Now().Add(10 * time.Second))
	h := make([]byte, 2)
	if _, e := io.ReadFull(c, h); e != nil || h[0] != 5 || h[1] == 0 {
		return
	}
	methods := make([]byte, int(h[1]))
	if _, e := io.ReadFull(c, methods); e != nil {
		return
	}
	supported := false
	for _, m := range methods {
		if m == 0 {
			supported = true
		}
	}
	if !supported {
		c.Write([]byte{5, 255})
		return
	}
	if _, e := c.Write([]byte{5, 0}); e != nil {
		return
	}
	req := make([]byte, 4)
	if _, e := io.ReadFull(c, req); e != nil {
		return
	}
	fail := func() { c.Write([]byte{5, 2, 0, 1, 0, 0, 0, 0, 0, 0}) }
	if req[0] != 5 || req[1] != 1 || req[2] != 0 || req[3] != 3 {
		fail()
		return
	}
	size := make([]byte, 1)
	if _, e := io.ReadFull(c, size); e != nil {
		return
	}
	target := make([]byte, int(size[0])+2)
	if _, e := io.ReadFull(c, target); e != nil {
		return
	}
	host := strings.ToLower(string(target[:len(target)-2]))
	port := binary.BigEndian.Uint16(target[len(target)-2:])
	if host != "api.telegram.org" || port != 443 {
		fail()
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cd, ok := d.(proxy.ContextDialer)
	if !ok {
		fail()
		return
	}
	up, e := cd.DialContext(ctx, "tcp", "api.telegram.org:443")
	if e != nil {
		fail()
		return
	}
	defer up.Close()
	if _, e = c.Write([]byte{5, 0, 0, 1, 0, 0, 0, 0, 0, 0}); e != nil {
		return
	}
	c.SetDeadline(time.Now().Add(5 * time.Minute))
	up.SetDeadline(time.Now().Add(5 * time.Minute))
	done := make(chan struct{}, 1)
	go func() {
		io.Copy(up, c)
		if tcp, ok := up.(*net.TCPConn); ok {
			tcp.CloseWrite()
		}
		done <- struct{}{}
	}()
	io.Copy(c, up)
	c.Close()
	up.Close()
	<-done
}
