/*
Copyright 2021 Gravitational, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package utils

import (
	"github.com/gravitational/trace"
)

// CircularBuffer implements an in-memory circular buffer of a predefined size
// holding float64 values.
type CircularBuffer struct {
	// buf holds the buffered values. Its length is fixed at construction time.
	buf []float64
	// next is the index of buf that the next added value will be written to.
	next int
	// count is the number of values currently stored, capped by len(buf).
	count int
}

// NewCircularBuffer returns a new instance of a circular buffer that
// holds size elements before rotating. A size that is not strictly
// positive is rejected with an error.
func NewCircularBuffer(size int) (*CircularBuffer, error) {
	if size <= 0 {
		return nil, trace.BadParameter("circular buffer size must be greater than zero, got %d", size)
	}
	return &CircularBuffer{
		buf: make([]float64, size),
	}, nil
}

// Add inserts a float64 value into the buffer. While the buffer is not
// yet full the value is stored in the next free slot; once it is full
// the value replaces the oldest stored value.
func (c *CircularBuffer) Add(d float64) {
	c.buf[c.next] = d
	c.next = (c.next + 1) % len(c.buf)
	if c.count < len(c.buf) {
		c.count++
	}
}

// Data returns the most recent n elements in insertion order, oldest
// first and newest last. If fewer than n values have been stored, all
// stored values are returned. If n is not positive, or nothing has been
// stored yet, nil is returned.
func (c *CircularBuffer) Data(n int) []float64 {
	if n <= 0 || c.count == 0 {
		return nil
	}
	if n > c.count {
		n = c.count
	}

	size := len(c.buf)
	// Index of the oldest stored value.
	oldest := (c.next - c.count + size) % size
	// Skip the values that are older than the requested window.
	start := (oldest + c.count - n) % size

	out := make([]float64, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, c.buf[(start+i)%size])
	}
	return out
}
