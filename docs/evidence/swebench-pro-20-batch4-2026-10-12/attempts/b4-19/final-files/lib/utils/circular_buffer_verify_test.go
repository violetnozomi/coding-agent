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
	"testing"

	"github.com/stretchr/testify/require"
)

func TestCircularBufferVerify(t *testing.T) {
	// invalid sizes
	for _, size := range []int{0, -1, -100} {
		buf, err := NewCircularBuffer(size)
		require.Error(t, err)
		require.Nil(t, buf)
	}

	// valid construction
	buf, err := NewCircularBuffer(3)
	require.NoError(t, err)
	require.NotNil(t, buf)
	require.Len(t, buf.buf, 3)

	// empty buffer returns nil for any n
	require.Nil(t, buf.Data(1))
	require.Nil(t, buf.Data(0))
	require.Nil(t, buf.Data(-3))

	// partially filled
	buf.Add(1)
	buf.Add(2)
	require.Equal(t, []float64{2}, buf.Data(1))
	require.Equal(t, []float64{1, 2}, buf.Data(2))
	require.Equal(t, []float64{1, 2}, buf.Data(10))

	// full and rotating
	buf.Add(3)
	require.Equal(t, []float64{1, 2, 3}, buf.Data(3))
	buf.Add(4)
	require.Equal(t, []float64{4}, buf.Data(1))
	require.Equal(t, []float64{3, 4}, buf.Data(2))
	require.Equal(t, []float64{2, 3, 4}, buf.Data(3))
	require.Equal(t, []float64{2, 3, 4}, buf.Data(100))

	// many rotations
	for i := 5; i <= 10; i++ {
		buf.Add(float64(i))
	}
	require.Equal(t, []float64{9, 10}, buf.Data(2))
	require.Equal(t, []float64{8, 9, 10}, buf.Data(3))

	// size 1 buffer
	one, err := NewCircularBuffer(1)
	require.NoError(t, err)
	one.Add(7)
	require.Equal(t, []float64{7}, one.Data(5))
	one.Add(8)
	require.Equal(t, []float64{8}, one.Data(5))
}
