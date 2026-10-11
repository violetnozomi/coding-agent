package main

import (
	"fmt"
	"time"

	"github.com/jellydator/ttlcache/v3"
)

func main() {
	c := ttlcache.New[string, string](ttlcache.WithDisableTouchOnHit[string, string]())
	it := c.Set("a", "1", ttlcache.DefaultTTL)
	fmt.Println("noTTL(DefaultTTL) expiresAt zero?", it.ExpiresAt().IsZero(), it.ExpiresAt())
	fmt.Println("TTL:", it.TTL())

	c2 := ttlcache.New[string, string](ttlcache.WithDisableTouchOnHit[string, string](), ttlcache.WithTTL[string, string](10*time.Millisecond))
	it2 := c2.Set("a", "1", ttlcache.DefaultTTL)
	fmt.Println("defaultTTL expiresAt zero?", it2.ExpiresAt().IsZero(), it2.ExpiresAt(), it2.TTL())

	it3 := c.Set("b", "2", 10*time.Millisecond)
	fmt.Println("explicit10ms expiresAt zero?", it3.ExpiresAt().IsZero(), "...")

	// Keys after expiry without DeleteExpired
	c3 := ttlcache.New[string, string](ttlcache.WithDisableTouchOnHit[string, string]())
	c3.Set("x", "1", 10*time.Millisecond)
	time.Sleep(30 * time.Millisecond)
	fmt.Println("Keys after expiry (no DeleteExpired):", c3.Keys())
	fmt.Println("Get after expiry:", c3.Get("x"))
	fmt.Println("Keys after Get:", c3.Keys())
	fmt.Println("Items len after Get:", len(c3.Items()))
}
