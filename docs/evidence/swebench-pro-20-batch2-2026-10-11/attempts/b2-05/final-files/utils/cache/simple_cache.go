package cache

import (
	"errors"
	"sync/atomic"
	"time"

	"github.com/jellydator/ttlcache/v3"
)

type SimpleCache[K comparable, V any] interface {
	Add(key K, value V) error
	AddWithTTL(key K, value V, ttl time.Duration) error
	Get(key K) (V, error)
	GetWithLoader(key K, loader func(key K) (V, time.Duration, error)) (V, error)
	Keys() []K
	Values() []V
}

type Options struct {
	SizeLimit  uint64
	DefaultTTL time.Duration
}

// evictionInterval is the minimum amount of time between two consecutive
// opportunistic evictions of expired items. It throttles how often the
// (relatively expensive) sweep of the internal cache is performed.
const evictionInterval = 10 * time.Millisecond

func NewSimpleCache[K comparable, V any](options ...Options) SimpleCache[K, V] {
	opts := []ttlcache.Option[K, V]{
		ttlcache.WithDisableTouchOnHit[K, V](),
	}
	if len(options) > 0 {
		o := options[0]
		if o.SizeLimit > 0 {
			opts = append(opts, ttlcache.WithCapacity[K, V](o.SizeLimit))
		}
		if o.DefaultTTL > 0 {
			opts = append(opts, ttlcache.WithTTL[K, V](o.DefaultTTL))
		}
	}

	c := ttlcache.New[K, V](opts...)
	return &simpleCache[K, V]{
		data: c,
	}
}

type simpleCache[K comparable, V any] struct {
	data *ttlcache.Cache[K, V]
	// evictionDeadline holds the earliest time at which the next opportunistic
	// eviction may run, throttling how often expired items are swept.
	evictionDeadline atomic.Pointer[time.Time]
}

// evictExpired removes expired items from the underlying cache. It is
// opportunistic: it only performs the sweep when the previous eviction
// deadline has elapsed, so that frequent cache operations do not trigger an
// eviction each time.
func (c *simpleCache[K, V]) evictExpired() {
	now := time.Now()
	deadline := c.evictionDeadline.Load()
	if deadline != nil && now.Before(*deadline) {
		return
	}
	c.data.DeleteExpired()
	next := now.Add(evictionInterval)
	c.evictionDeadline.Store(&next)
}

func (c *simpleCache[K, V]) Add(key K, value V) error {
	return c.AddWithTTL(key, value, ttlcache.DefaultTTL)
}

func (c *simpleCache[K, V]) AddWithTTL(key K, value V, ttl time.Duration) error {
	c.evictExpired()
	item := c.data.Set(key, value, ttl)
	if item == nil {
		return errors.New("failed to add item")
	}
	return nil
}

func (c *simpleCache[K, V]) Get(key K) (V, error) {
	c.evictExpired()
	item := c.data.Get(key)
	if item == nil {
		var zero V
		return zero, errors.New("item not found")
	}
	return item.Value(), nil
}

func (c *simpleCache[K, V]) GetWithLoader(key K, loader func(key K) (V, time.Duration, error)) (V, error) {
	c.evictExpired()
	loaderWrapper := ttlcache.LoaderFunc[K, V](
		func(t *ttlcache.Cache[K, V], key K) *ttlcache.Item[K, V] {
			value, ttl, err := loader(key)
			if err != nil {
				return nil
			}
			return t.Set(key, value, ttl)
		},
	)
	item := c.data.Get(key, ttlcache.WithLoader[K, V](loaderWrapper))
	if item == nil {
		var zero V
		return zero, errors.New("item not found")
	}
	return item.Value(), nil
}

// Keys returns the keys of all active (non-expired) items in the cache.
func (c *simpleCache[K, V]) Keys() []K {
	c.evictExpired()
	items := c.data.Items()
	keys := make([]K, 0, len(items))
	for key := range items {
		keys = append(keys, key)
	}
	return keys
}

// Values returns the values of all active (non-expired) items in the cache.
// Its behavior mirrors Keys, so identifiers and values always reflect the
// same set of live entries.
func (c *simpleCache[K, V]) Values() []V {
	c.evictExpired()
	items := c.data.Items()
	values := make([]V, 0, len(items))
	for _, item := range items {
		values = append(values, item.Value())
	}
	return values
}
