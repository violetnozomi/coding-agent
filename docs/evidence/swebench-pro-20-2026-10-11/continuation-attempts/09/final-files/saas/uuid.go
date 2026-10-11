package saas

import (
	"bytes"
	"fmt"
	"io/ioutil"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"

	"github.com/BurntSushi/toml"
	c "github.com/future-architect/vuls/config"
	"github.com/future-architect/vuls/models"
	"github.com/future-architect/vuls/util"
	"github.com/hashicorp/go-uuid"
	"golang.org/x/xerrors"
)

const reUUID = "[\\da-f]{8}-[\\da-f]{4}-[\\da-f]{4}-[\\da-f]{4}-[\\da-f]{12}"

// Scanning with the -containers-only flag at scan time, the UUID of Container Host may not be generated,
// so check it. Otherwise create a UUID of the Container Host and set it.
func getOrCreateServerUUID(r models.ScanResult, server c.ServerInfo) (serverUUID string, err error) {
	if id, ok := server.UUIDs[r.ServerName]; !ok {
		if serverUUID, err = uuid.GenerateUUID(); err != nil {
			return "", xerrors.Errorf("Failed to generate UUID: %w", err)
		}
	} else {
		matched, err := regexp.MatchString(reUUID, id)
		if !matched || err != nil {
			if serverUUID, err = uuid.GenerateUUID(); err != nil {
				return "", xerrors.Errorf("Failed to generate UUID: %w", err)
			}
		}
	}
	return serverUUID, nil
}

// EnsureUUIDs generate a new UUID of the scan target server if UUID is not assigned yet.
// And then set the generated UUID to config.toml and scan results.
func EnsureUUIDs(configPath string, results models.ScanResults) (err error) {
	// Sort Host->Container
	sort.Slice(results, func(i, j int) bool {
		if results[i].ServerName == results[j].ServerName {
			return results[i].Container.ContainerID < results[j].Container.ContainerID
		}
		return results[i].ServerName < results[j].ServerName
	})

	needsOverwrite, err := ensure(c.Conf.Servers, configPath, results, uuid.GenerateUUID)
	if err != nil {
		return xerrors.Errorf("Failed to ensure UUIDs: %w", err)
	} else if !needsOverwrite {
		// Every valid UUID is already assigned to the scan results, so there is
		// no need to regenerate a UUID or to overwrite config.toml.
		return nil
	}

	for name, server := range c.Conf.Servers {
		server = cleanForTOMLEncoding(server, c.Conf.Default)
		c.Conf.Servers[name] = server
	}
	if c.Conf.Default.WordPress != nil && c.Conf.Default.WordPress.IsZero() {
		c.Conf.Default.WordPress = nil
	}

	c := struct {
		Saas    *c.SaasConf             `toml:"saas"`
		Default c.ServerInfo            `toml:"default"`
		Servers map[string]c.ServerInfo `toml:"servers"`
	}{
		Saas:    &c.Conf.Saas,
		Default: c.Conf.Default,
		Servers: c.Conf.Servers,
	}

	// rename the current config.toml to config.toml.bak
	info, err := os.Lstat(configPath)
	if err != nil {
		return xerrors.Errorf("Failed to lstat %s: %w", configPath, err)
	}
	realPath := configPath
	if info.Mode()&os.ModeSymlink == os.ModeSymlink {
		if realPath, err = os.Readlink(configPath); err != nil {
			return xerrors.Errorf("Failed to Read link %s: %w", configPath, err)
		}
	}
	if err := os.Rename(realPath, realPath+".bak"); err != nil {
		return xerrors.Errorf("Failed to rename %s: %w", configPath, err)
	}

	var buf bytes.Buffer
	if err := toml.NewEncoder(&buf).Encode(c); err != nil {
		return xerrors.Errorf("Failed to encode to toml: %w", err)
	}
	str := strings.Replace(buf.String(), "\n  [", "\n\n  [", -1)
	str = fmt.Sprintf("%s\n\n%s",
		"# See README for details: https://vuls.io/docs/en/usage-settings.html",
		str)

	return ioutil.WriteFile(realPath, []byte(str), 0600)
}

// ensure assigns UUIDs to the scan results of hosts and containers.
//
// For a host, the UUID is stored in the per-server UUIDs map under the key
// equal to the host's ServerName. For a container, the UUID is stored under
// "<containerName>@<serverName>" and the UUID of the container's host is also
// ensured under the ServerName key.
//
// An existing UUID that matches reUUID is reused as is and is never
// regenerated. When a UUID is missing or invalid, generateFunc is used to
// create a new one, which is stored in servers and applied to scanResults.
//
// It mutates servers and scanResults in place and returns needsOverwrite=true
// when at least one UUID was added or replaced. It returns needsOverwrite=false
// when every required UUID was already present and valid.
func ensure(servers map[string]c.ServerInfo, path string, scanResults models.ScanResults, generateFunc func() (string, error)) (needsOverwrite bool, err error) {
	re := regexp.MustCompile(reUUID)
	for i, r := range scanResults {
		server, found := servers[r.ServerName]
		if !found {
			util.Log.Warnf("The server %s is not found in %s", r.ServerName, path)
		}
		if server.UUIDs == nil {
			server.UUIDs = map[string]string{}
		}

		if r.IsContainer() {
			// The UUID of the container's host is required as well.
			if id, ok := server.UUIDs[r.ServerName]; ok && re.MatchString(id) {
				scanResults[i].ServerUUID = id
			} else {
				if ok {
					util.Log.Warnf("UUID of %s is invalid in %s. Re-generate UUID: %s", r.ServerName, path, id)
				}
				id, err := generateFunc()
				if err != nil {
					return false, xerrors.Errorf("Failed to generate UUID: %w", err)
				}
				server.UUIDs[r.ServerName] = id
				scanResults[i].ServerUUID = id
				needsOverwrite = true
			}

			name := fmt.Sprintf("%s@%s", r.Container.Name, r.ServerName)
			if id, ok := server.UUIDs[name]; ok && re.MatchString(id) {
				scanResults[i].Container.UUID = id
			} else {
				if ok {
					util.Log.Warnf("UUID of %s is invalid in %s. Re-generate UUID: %s", r.Container.Name, path, id)
				}
				id, err := generateFunc()
				if err != nil {
					return false, xerrors.Errorf("Failed to generate UUID: %w", err)
				}
				server.UUIDs[name] = id
				scanResults[i].Container.UUID = id
				needsOverwrite = true
			}
		} else {
			if id, ok := server.UUIDs[r.ServerName]; ok && re.MatchString(id) {
				scanResults[i].ServerUUID = id
			} else {
				if ok {
					util.Log.Warnf("UUID of %s is invalid in %s. Re-generate UUID: %s", r.ServerName, path, id)
				}
				id, err := generateFunc()
				if err != nil {
					return false, xerrors.Errorf("Failed to generate UUID: %w", err)
				}
				server.UUIDs[r.ServerName] = id
				scanResults[i].ServerUUID = id
				needsOverwrite = true
			}
		}

		servers[r.ServerName] = server
	}
	return needsOverwrite, nil
}

func cleanForTOMLEncoding(server c.ServerInfo, def c.ServerInfo) c.ServerInfo {
	if reflect.DeepEqual(server.Optional, def.Optional) {
		server.Optional = nil
	}

	if def.User == server.User {
		server.User = ""
	}

	if def.Host == server.Host {
		server.Host = ""
	}

	if def.Port == server.Port {
		server.Port = ""
	}

	if def.KeyPath == server.KeyPath {
		server.KeyPath = ""
	}

	if reflect.DeepEqual(server.ScanMode, def.ScanMode) {
		server.ScanMode = nil
	}

	if def.Type == server.Type {
		server.Type = ""
	}

	if reflect.DeepEqual(server.CpeNames, def.CpeNames) {
		server.CpeNames = nil
	}

	if def.OwaspDCXMLPath == server.OwaspDCXMLPath {
		server.OwaspDCXMLPath = ""
	}

	if reflect.DeepEqual(server.IgnoreCves, def.IgnoreCves) {
		server.IgnoreCves = nil
	}

	if reflect.DeepEqual(server.Enablerepo, def.Enablerepo) {
		server.Enablerepo = nil
	}

	for k, v := range def.Optional {
		if vv, ok := server.Optional[k]; ok && v == vv {
			delete(server.Optional, k)
		}
	}

	if server.WordPress != nil {
		if server.WordPress.IsZero() || reflect.DeepEqual(server.WordPress, def.WordPress) {
			server.WordPress = nil
		}
	}

	return server
}
