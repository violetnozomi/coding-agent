package saas

import (
	"errors"
	"testing"

	"github.com/future-architect/vuls/config"
	"github.com/future-architect/vuls/models"
)

const defaultUUID = "11111111-1111-1111-1111-111111111111"

func TestGetOrCreateServerUUID(t *testing.T) {

	cases := map[string]struct {
		scanResult models.ScanResult
		server     config.ServerInfo
		isDefault  bool
	}{
		"baseServer": {
			scanResult: models.ScanResult{
				ServerName: "hoge",
			},
			server: config.ServerInfo{
				UUIDs: map[string]string{
					"hoge": defaultUUID,
				},
			},
			isDefault: false,
		},
		"onlyContainers": {
			scanResult: models.ScanResult{
				ServerName: "hoge",
			},
			server: config.ServerInfo{
				UUIDs: map[string]string{
					"fuga": defaultUUID,
				},
			},
			isDefault: false,
		},
	}

	for testcase, v := range cases {
		uuid, err := getOrCreateServerUUID(v.scanResult, v.server)
		if err != nil {
			t.Errorf("%s", err)
		}
		if (uuid == defaultUUID) != v.isDefault {
			t.Errorf("%s : expected isDefault %t got %s", testcase, v.isDefault, uuid)
		}
	}
}

func TestEnsure(t *testing.T) {
	const (
		hostUUID      = "11111111-1111-1111-1111-111111111111"
		containerUUID = "22222222-2222-2222-2222-222222222222"
		generated     = "33333333-3333-3333-3333-333333333333"
		invalid       = "not-a-uuid"
	)

	generate := func() (string, error) { return generated, nil }

	cases := map[string]struct {
		servers        map[string]config.ServerInfo
		scanResults    models.ScanResults
		expectedServer map[string]string
		expectedSrvUID string
		expectedCtrUID string
		expectedOvr    bool
	}{
		"host with valid uuid is reused": {
			servers: map[string]config.ServerInfo{
				"hoge": {UUIDs: map[string]string{"hoge": hostUUID}},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge"},
			},
			expectedServer: map[string]string{"hoge": hostUUID},
			expectedSrvUID: hostUUID,
			expectedOvr:    false,
		},
		"host with missing uuid is generated": {
			servers: map[string]config.ServerInfo{
				"hoge": {},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge"},
			},
			expectedServer: map[string]string{"hoge": generated},
			expectedSrvUID: generated,
			expectedOvr:    true,
		},
		"host with invalid uuid is regenerated": {
			servers: map[string]config.ServerInfo{
				"hoge": {UUIDs: map[string]string{"hoge": invalid}},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge"},
			},
			expectedServer: map[string]string{"hoge": generated},
			expectedSrvUID: generated,
			expectedOvr:    true,
		},
		"container with valid uuids is reused": {
			servers: map[string]config.ServerInfo{
				"hoge": {UUIDs: map[string]string{
					"hoge":       hostUUID,
					"fuga@hoge": containerUUID,
				}},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge", Container: models.Container{ContainerID: "abc", Name: "fuga"}},
			},
			expectedServer: map[string]string{"hoge": hostUUID, "fuga@hoge": containerUUID},
			expectedSrvUID: hostUUID,
			expectedCtrUID: containerUUID,
			expectedOvr:    false,
		},
		"container with missing uuids is generated": {
			servers: map[string]config.ServerInfo{
				"hoge": {},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge", Container: models.Container{ContainerID: "abc", Name: "fuga"}},
			},
			expectedServer: map[string]string{"hoge": generated, "fuga@hoge": generated},
			expectedSrvUID: generated,
			expectedCtrUID: generated,
			expectedOvr:    true,
		},
		"container with invalid container uuid is regenerated": {
			servers: map[string]config.ServerInfo{
				"hoge": {UUIDs: map[string]string{
					"hoge":       hostUUID,
					"fuga@hoge": invalid,
				}},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge", Container: models.Container{ContainerID: "abc", Name: "fuga"}},
			},
			expectedServer: map[string]string{"hoge": hostUUID, "fuga@hoge": generated},
			expectedSrvUID: hostUUID,
			expectedCtrUID: generated,
			expectedOvr:    true,
		},
		"host and container mixed": {
			servers: map[string]config.ServerInfo{
				"hoge": {UUIDs: map[string]string{
					"hoge":       hostUUID,
					"fuga@hoge": containerUUID,
				}},
			},
			scanResults: models.ScanResults{
				{ServerName: "hoge"},
				{ServerName: "hoge", Container: models.Container{ContainerID: "abc", Name: "fuga"}},
			},
			expectedServer: map[string]string{"hoge": hostUUID, "fuga@hoge": containerUUID},
			expectedOvr:    false,
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			needsOverwrite, err := ensure(tc.servers, "config.toml", tc.scanResults, generate)
			if err != nil {
				t.Fatalf("unexpected error: %+v", err)
			}
			if needsOverwrite != tc.expectedOvr {
				t.Errorf("needsOverwrite: expected %t, got %t", tc.expectedOvr, needsOverwrite)
			}
			for k, want := range tc.expectedServer {
				if got := tc.servers["hoge"].UUIDs[k]; got != want {
					t.Errorf("servers[hoge].UUIDs[%s]: expected %s, got %s", k, want, got)
				}
			}
			if tc.expectedSrvUID != "" && tc.scanResults[0].ServerUUID != tc.expectedSrvUID {
				t.Errorf("ServerUUID: expected %s, got %s", tc.expectedSrvUID, tc.scanResults[0].ServerUUID)
			}
			if tc.expectedCtrUID != "" && tc.scanResults[0].Container.UUID != tc.expectedCtrUID {
				t.Errorf("Container.UUID: expected %s, got %s", tc.expectedCtrUID, tc.scanResults[0].Container.UUID)
			}
		})
	}
}

func TestEnsureGenerateError(t *testing.T) {
	servers := map[string]config.ServerInfo{"hoge": {}}
	results := models.ScanResults{{ServerName: "hoge"}}
	_, err := ensure(servers, "config.toml", results, func() (string, error) {
		return "", errors.New("generate failed")
	})
	if err == nil {
		t.Fatal("expected an error when UUID generation fails")
	}
}
