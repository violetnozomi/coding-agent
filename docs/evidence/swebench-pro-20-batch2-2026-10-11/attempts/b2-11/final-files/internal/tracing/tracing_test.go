package tracing

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.flipt.io/flipt/internal/config"
	semconv "go.opentelemetry.io/otel/semconv/v1.4.0"
)

func TestNewResource(t *testing.T) {
	res, err := newResource(context.Background(), "1.0.0")
	require.NoError(t, err)

	attrs := make(map[string]string)
	for _, kv := range res.Attributes() {
		attrs[string(kv.Key)] = kv.Value.Emit()
	}

	assert.Equal(t, "flipt", attrs[string(semconv.ServiceNameKey)])
	assert.Equal(t, "1.0.0", attrs[string(semconv.ServiceVersionKey)])
}

func TestNewResourceFromEnv(t *testing.T) {
	t.Setenv("OTEL_SERVICE_NAME", "flipt-from-env")
	t.Setenv("OTEL_RESOURCE_ATTRIBUTES", "foo=bar,baz=qux")

	res, err := newResource(context.Background(), "1.0.0")
	require.NoError(t, err)

	attrs := make(map[string]string)
	for _, kv := range res.Attributes() {
		attrs[string(kv.Key)] = kv.Value.Emit()
	}

	assert.Equal(t, "flipt-from-env", attrs[string(semconv.ServiceNameKey)])
	assert.Equal(t, "bar", attrs["foo"])
	assert.Equal(t, "qux", attrs["baz"])
}

func TestNewProvider(t *testing.T) {
	provider, err := NewProvider(context.Background(), "1.0.0")
	require.NoError(t, err)
	require.NotNil(t, provider)
}

func TestGetExporter(t *testing.T) {
	tests := []struct {
		name    string
		cfg     *config.TracingConfig
		wantErr error
	}{
		{
			name: "Jaeger",
			cfg: &config.TracingConfig{
				Exporter: config.TracingJaeger,
				Jaeger: config.JaegerTracingConfig{
					Host: "localhost",
					Port: 6831,
				},
			},
		},
		{
			name: "Zipkin",
			cfg: &config.TracingConfig{
				Exporter: config.TracingZipkin,
				Zipkin: config.ZipkinTracingConfig{
					Endpoint: "http://localhost:9411/api/v2/spans",
				},
			},
		},
		{
			name: "OTLP HTTP",
			cfg: &config.TracingConfig{
				Exporter: config.TracingOTLP,
				OTLP: config.OTLPTracingConfig{
					Endpoint: "http://localhost:4317",
					Headers:  map[string]string{"key": "value"},
				},
			},
		},
		{
			name: "OTLP HTTPS",
			cfg: &config.TracingConfig{
				Exporter: config.TracingOTLP,
				OTLP: config.OTLPTracingConfig{
					Endpoint: "https://localhost:4317",
					Headers:  map[string]string{"key": "value"},
				},
			},
		},
		{
			name: "OTLP GRPC",
			cfg: &config.TracingConfig{
				Exporter: config.TracingOTLP,
				OTLP: config.OTLPTracingConfig{
					Endpoint: "grpc://localhost:4317",
					Headers:  map[string]string{"key": "value"},
				},
			},
		},
		{
			name: "OTLP default",
			cfg: &config.TracingConfig{
				Exporter: config.TracingOTLP,
				OTLP: config.OTLPTracingConfig{
					Endpoint: "localhost:4317",
					Headers:  map[string]string{"key": "value"},
				},
			},
		},
		{
			name:    "Unsupported Exporter",
			cfg:     &config.TracingConfig{},
			wantErr: errors.New("unsupported tracing exporter: "),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// reset the memoised construction so each case is exercised
			traceExpOnce = sync.Once{}
			traceExp, traceExpFunc, traceExpErr = nil, nil, nil

			exp, expFunc, err := GetExporter(context.Background(), tt.cfg)
			if tt.wantErr != nil {
				require.Error(t, err)
				assert.ErrorContains(t, err, tt.wantErr.Error())
				return
			}
			require.NoError(t, err)
			assert.NotNil(t, exp)
			assert.NotNil(t, expFunc)

			t.Cleanup(func() {
				assert.NoError(t, expFunc(context.Background()))
			})
		})
	}
}
