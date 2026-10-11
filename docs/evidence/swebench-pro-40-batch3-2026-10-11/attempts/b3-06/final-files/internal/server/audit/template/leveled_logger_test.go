package template

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

func TestNewLeveledLogger(t *testing.T) {
	core, logs := observer.New(zapcore.DebugLevel)

	logger := NewLeveledLogger(zap.New(core))
	require.NotNil(t, logger)

	logger.Debug("debug message", "debug-key", "debug-value")
	logger.Info("info message", "info-key", 1)
	logger.Warn("warn message", "warn-key", true)
	logger.Error("error message", "error-key", 1.5, "extra", "value")

	entries := logs.All()
	require.Len(t, entries, 4)

	assert.Equal(t, "DEBUG", entries[0].Level.CapitalString())
	assert.Equal(t, "debug message", entries[0].Message)
	assert.Equal(t, map[string]any{"debug-key": "debug-value"}, entries[0].ContextMap())

	assert.Equal(t, "INFO", entries[1].Level.CapitalString())
	assert.Equal(t, "info message", entries[1].Message)
	assert.Equal(t, map[string]any{"info-key": int64(1)}, entries[1].ContextMap())

	assert.Equal(t, "WARN", entries[2].Level.CapitalString())
	assert.Equal(t, "warn message", entries[2].Message)
	assert.Equal(t, map[string]any{"warn-key": true}, entries[2].ContextMap())

	assert.Equal(t, "ERROR", entries[3].Level.CapitalString())
	assert.Equal(t, "error message", entries[3].Message)
	assert.Equal(t, map[string]any{"error-key": 1.5, "extra": "value"}, entries[3].ContextMap())
}

func TestLeveledLogger_DisabledLevels(t *testing.T) {
	core, logs := observer.New(zapcore.WarnLevel)

	logger := NewLeveledLogger(zap.New(core))

	logger.Debug("debug message", "key", "value")
	logger.Info("info message", "key", "value")

	assert.Empty(t, logs.All())

	logger.Warn("warn message", "key", "value")
	logger.Error("error message", "key", "value")

	require.Len(t, logs.All(), 2)
}

func TestLeveledLogger_KeyValueOrder(t *testing.T) {
	core, logs := observer.New(zapcore.InfoLevel)

	logger := NewLeveledLogger(zap.New(core))
	logger.Info("message", "a", "1", "b", "2", "c", "3")

	entries := logs.All()
	require.Len(t, entries, 1)

	var keys []string
	for _, field := range entries[0].Context {
		keys = append(keys, field.Key)
	}

	assert.Equal(t, []string{"a", "b", "c"}, keys)
}

func TestLeveledLogger_NoKeyValues(t *testing.T) {
	core, logs := observer.New(zapcore.InfoLevel)

	logger := NewLeveledLogger(zap.New(core))
	logger.Info("plain message")

	entries := logs.All()
	require.Len(t, entries, 1)
	assert.Empty(t, entries[0].Context)
	assert.Equal(t, "plain message", entries[0].Message)
}
