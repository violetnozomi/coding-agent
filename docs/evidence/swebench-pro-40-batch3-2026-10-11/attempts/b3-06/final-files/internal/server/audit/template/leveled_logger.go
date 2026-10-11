package template

import (
	"fmt"

	"github.com/hashicorp/go-retryablehttp"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// LeveledLogger is a zap-backed implementation of retryablehttp.LeveledLogger.
//
// The retry client shipped by go-retryablehttp only accepts a value that
// implements either its Logger or LeveledLogger interface. Passing a raw
// *zap.Logger makes the client panic with "invalid logger type passed" as soon
// as it tries to log a retry attempt, which takes the whole process down.
// Wrapping the zap logger in this adapter keeps the retry client happy while
// still emitting structured entries.
type LeveledLogger struct {
	logger *zap.Logger
}

// Ensure the adapter satisfies the interface expected by the retry client.
var _ retryablehttp.LeveledLogger = (*LeveledLogger)(nil)

// NewLeveledLogger wraps logger so that it can be used as the Logger field of a
// go-retryablehttp client.
func NewLeveledLogger(logger *zap.Logger) retryablehttp.LeveledLogger {
	return &LeveledLogger{logger: logger}
}

// Debug emits a single DEBUG entry when that level is enabled.
func (l *LeveledLogger) Debug(msg string, keyvals ...interface{}) {
	l.log(zapcore.DebugLevel, msg, keyvals...)
}

// Info emits a single INFO entry when that level is enabled.
func (l *LeveledLogger) Info(msg string, keyvals ...interface{}) {
	l.log(zapcore.InfoLevel, msg, keyvals...)
}

// Warn emits a single WARN entry when that level is enabled.
func (l *LeveledLogger) Warn(msg string, keyvals ...interface{}) {
	l.log(zapcore.WarnLevel, msg, keyvals...)
}

// Error emits a single ERROR entry when that level is enabled.
func (l *LeveledLogger) Error(msg string, keyvals ...interface{}) {
	l.log(zapcore.ErrorLevel, msg, keyvals...)
}

// log emits exactly one entry for level when it is enabled on the underlying
// core, and nothing at all when it is disabled.
//
// The message is handed to zap untouched (the level is never prepended). Each
// supplied key/value pair becomes a structured field, with the pairs rendered
// in the order they were supplied.
func (l *LeveledLogger) log(level zapcore.Level, msg string, keyvals ...interface{}) {
	if l.logger == nil || !l.logger.Core().Enabled(level) {
		return
	}

	fields := make([]zap.Field, 0, (len(keyvals)+1)/2)
	for i := 0; i < len(keyvals); i += 2 {
		key := fmt.Sprint(keyvals[i])

		var value interface{}
		if i+1 < len(keyvals) {
			value = keyvals[i+1]
		}

		fields = append(fields, zap.Any(key, value))
	}

	l.logger.Log(level, msg, fields...)
}
