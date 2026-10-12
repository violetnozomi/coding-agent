package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"

	"github.com/pkg/errors"
	"github.com/spf13/viper"
)

// Scheme is the protocol used to serve the Flipt HTTP API and UI.
type Scheme string

const (
	// HTTP serves the API and UI over plain HTTP.
	HTTP Scheme = "http"
	// HTTPS serves the API and UI over TLS.
	HTTPS Scheme = "https"
)

// String returns the string representation of the Scheme.
func (s Scheme) String() string {
	return string(s)
}

type config struct {
	LogLevel string         `json:"logLevel,omitempty"`
	UI       uiConfig       `json:"ui,omitempty"`
	Cors     corsConfig     `json:"cors,omitempty"`
	Cache    cacheConfig    `json:"cache,omitempty"`
	Server   serverConfig   `json:"server,omitempty"`
	Database databaseConfig `json:"database,omitempty"`
}

type uiConfig struct {
	Enabled bool `json:"enabled"`
}

type corsConfig struct {
	Enabled        bool     `json:"enabled"`
	AllowedOrigins []string `json:"allowedOrigins,omitempty"`
}

type memoryCacheConfig struct {
	Enabled bool `json:"enabled"`
	Items   int  `json:"items,omitempty"`
}

type cacheConfig struct {
	Memory memoryCacheConfig `json:"memory,omitempty"`
}

type serverConfig struct {
	Host      string `json:"host,omitempty" mapstructure:"host"`
	Protocol  Scheme `json:"protocol,omitempty" mapstructure:"protocol"`
	HTTPPort  int    `json:"httpPort,omitempty" mapstructure:"http_port"`
	HTTPSPort int    `json:"httpsPort,omitempty" mapstructure:"https_port"`
	GRPCPort  int    `json:"grpcPort,omitempty" mapstructure:"grpc_port"`
	CertFile  string `json:"certFile,omitempty" mapstructure:"cert_file"`
	CertKey   string `json:"certKey,omitempty" mapstructure:"cert_key"`
}

type databaseConfig struct {
	MigrationsPath string `json:"migrationsPath,omitempty"`
	URL            string `json:"url,omitempty"`
}

func defaultConfig() *config {
	return &config{
		LogLevel: "INFO",

		UI: uiConfig{
			Enabled: true,
		},

		Cors: corsConfig{
			Enabled:        false,
			AllowedOrigins: []string{"*"},
		},

		Cache: cacheConfig{
			Memory: memoryCacheConfig{
				Enabled: false,
				Items:   500,
			},
		},

		Server: serverConfig{
			Host:      "0.0.0.0",
			Protocol:  HTTP,
			HTTPPort:  8080,
			HTTPSPort: 443,
			GRPCPort:  9000,
		},

		Database: databaseConfig{
			URL:            "file:/var/opt/flipt/flipt.db",
			MigrationsPath: "/etc/flipt/config/migrations",
		},
	}
}

const (
	// Logging
	cfgLogLevel = "log.level"

	// UI
	cfgUIEnabled = "ui.enabled"

	// CORS
	cfgCorsEnabled        = "cors.enabled"
	cfgCorsAllowedOrigins = "cors.allowed_origins"

	// Cache
	cfgCacheMemoryEnabled = "cache.memory.enabled"
	cfgCacheMemoryItems   = "cache.memory.items"

	// Server
	cfgServerHost      = "server.host"
	cfgServerProtocol  = "server.protocol"
	cfgServerHTTPPort  = "server.http_port"
	cfgServerHTTPSPort = "server.https_port"
	cfgServerGRPCPort  = "server.grpc_port"
	cfgServerCertFile  = "server.cert_file"
	cfgServerCertKey   = "server.cert_key"

	// DB
	cfgDBURL            = "db.url"
	cfgDBMigrationsPath = "db.migrations.path"
)

func configure(path string) (*config, error) {
	v := viper.New()
	v.SetEnvPrefix("FLIPT")
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	v.AutomaticEnv()

	v.SetConfigFile(path)

	if err := v.ReadInConfig(); err != nil {
		return nil, errors.Wrap(err, "loading config")
	}

	cfg := defaultConfig()

	// Logging
	if v.IsSet(cfgLogLevel) {
		cfg.LogLevel = v.GetString(cfgLogLevel)
	}

	// UI
	if v.IsSet(cfgUIEnabled) {
		cfg.UI.Enabled = v.GetBool(cfgUIEnabled)
	}

	// CORS
	if v.IsSet(cfgCorsEnabled) {
		cfg.Cors.Enabled = v.GetBool(cfgCorsEnabled)
	}

	if v.IsSet(cfgCorsAllowedOrigins) {
		cfg.Cors.AllowedOrigins = v.GetStringSlice(cfgCorsAllowedOrigins)
	}

	// Cache
	if v.IsSet(cfgCacheMemoryEnabled) {
		cfg.Cache.Memory.Enabled = v.GetBool(cfgCacheMemoryEnabled)
	}

	if v.IsSet(cfgCacheMemoryItems) {
		cfg.Cache.Memory.Items = v.GetInt(cfgCacheMemoryItems)
	}

	// Server
	if v.IsSet(cfgServerHost) {
		cfg.Server.Host = v.GetString(cfgServerHost)
	}

	if v.IsSet(cfgServerProtocol) {
		switch Scheme(strings.ToLower(v.GetString(cfgServerProtocol))) {
		case HTTPS:
			cfg.Server.Protocol = HTTPS
		default:
			cfg.Server.Protocol = HTTP
		}
	}

	if v.IsSet(cfgServerHTTPPort) {
		cfg.Server.HTTPPort = v.GetInt(cfgServerHTTPPort)
	}

	if v.IsSet(cfgServerHTTPSPort) {
		cfg.Server.HTTPSPort = v.GetInt(cfgServerHTTPSPort)
	}

	if v.IsSet(cfgServerGRPCPort) {
		cfg.Server.GRPCPort = v.GetInt(cfgServerGRPCPort)
	}

	if v.IsSet(cfgServerCertFile) {
		cfg.Server.CertFile = v.GetString(cfgServerCertFile)
	}

	if v.IsSet(cfgServerCertKey) {
		cfg.Server.CertKey = v.GetString(cfgServerCertKey)
	}

	// DB
	if v.IsSet(cfgDBURL) {
		cfg.Database.URL = v.GetString(cfgDBURL)
	}

	if v.IsSet(cfgDBMigrationsPath) {
		cfg.Database.MigrationsPath = v.GetString(cfgDBMigrationsPath)
	}

	if err := cfg.validate(); err != nil {
		return nil, err
	}

	return cfg, nil
}

func (c *config) validate() error {
	if c.Server.Protocol == HTTPS {
		if c.Server.CertFile == "" {
			return errors.New("cert_file cannot be empty when using HTTPS")
		}

		if c.Server.CertKey == "" {
			return errors.New("cert_key cannot be empty when using HTTPS")
		}

		if _, err := os.Stat(c.Server.CertFile); os.IsNotExist(err) {
			return errors.Errorf("cannot find TLS cert_file at %q", c.Server.CertFile)
		}

		if _, err := os.Stat(c.Server.CertKey); os.IsNotExist(err) {
			return errors.Errorf("cannot find TLS cert_key at %q", c.Server.CertKey)
		}
	}

	return nil
}

func (c *config) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	out, err := json.Marshal(c)
	if err != nil {
		logger.WithError(err).Error("getting config")
		w.WriteHeader(http.StatusInternalServerError)
		return
	}

	if _, err = w.Write(out); err != nil {
		logger.WithError(err).Error("writing response")
		w.WriteHeader(http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
}

type info struct {
	Version   string `json:"version,omitempty"`
	Commit    string `json:"commit,omitempty"`
	BuildDate string `json:"buildDate,omitempty"`
	GoVersion string `json:"goVersion,omitempty"`
}

func (i info) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	out, err := json.Marshal(i)
	if err != nil {
		logger.WithError(err).Error("getting metadata")
		w.WriteHeader(http.StatusInternalServerError)
		return
	}

	if _, err = w.Write(out); err != nil {
		logger.WithError(err).Error("writing response")
		w.WriteHeader(http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
}
