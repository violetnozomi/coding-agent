package parser

import (
	"testing"

	"github.com/aquasecurity/trivy/pkg/types"
	"github.com/future-architect/vuls/models"
)

func TestIsTrivySupportedOS(t *testing.T) {
	for _, family := range []string{
		"alpine", "debian", "ubuntu", "centos", "redhat", "fedora",
		"amazon", "oracle", "photon", "opensuse", "opensuse.leap",
		"opensuse.tumbleweed", "suse linux enterprise server", "windows",
	} {
		if !IsTrivySupportedOS(family) {
			t.Errorf("expected %q to be a supported OS", family)
		}
	}
	for _, family := range []string{"", "npm", "composer", "pipenv", "bundler", "cargo", "unknown"} {
		if IsTrivySupportedOS(family) {
			t.Errorf("expected %q not to be a supported OS", family)
		}
	}
}

func TestParseInvalidJSON(t *testing.T) {
	res, err := Parse([]byte(`{invalid`), &models.ScanResult{})
	if err == nil {
		t.Errorf("expected an error")
	}
	if res != nil {
		t.Errorf("expected nil result, got %#v", res)
	}
}

func TestParse(t *testing.T) {
	vulnJSON := []byte(`[
	  {
	    "Target": "library",
	    "Type": "npm",
	    "Vulnerabilities": [
	      {
	        "VulnerabilityID": "CVE-2020-0001",
	        "PkgName": "bad-lib",
	        "InstalledVersion": "1.0.0",
	        "FixedVersion": "1.1.0",
	        "Title": "lib title",
	        "Description": "lib description",
	        "Severity": "HIGH",
	        "References": ["https://b.example.com", "https://a.example.com"]
	      },
	      {
	        "VulnerabilityID": "RUSTSEC-2020-0002",
	        "PkgName": "rust-lib",
	        "InstalledVersion": "0.1.0",
	        "Title": "rust title",
	        "Description": "rust description",
	        "Severity": "MEDIUM"
	      }
	    ]
	  },
	  {
	    "Target": "library",
	    "Type": "npm",
	    "Vulnerabilities": [
	      {
	        "VulnerabilityID": "CVE-2020-0001",
	        "PkgName": "bad-lib",
	        "InstalledVersion": "1.0.0",
	        "FixedVersion": "1.1.0",
	        "Title": "lib title",
	        "Description": "lib description",
	        "Severity": "HIGH"
	      },
	      {
	        "VulnerabilityID": "CVE-2020-0001",
	        "PkgName": "another-lib",
	        "InstalledVersion": "2.0.0",
	        "FixedVersion": "2.1.0"
	      }
	    ]
	  },
	  {
	    "Target": "debian:10",
	    "Type": "debian",
	    "Vulnerabilities": [
	      {
	        "VulnerabilityID": "CVE-2020-0003",
	        "PkgName": "os-pkg",
	        "InstalledVersion": "3.0-1",
	        "FixedVersion": "3.0-2",
	        "Title": "os title",
	        "Description": "os description",
	        "Severity": "CRITICAL"
	      }
	    ]
	  },
	  {
	    "Target": "unknown-target",
	    "Type": "unknown",
	    "Vulnerabilities": [
	      {
	        "VulnerabilityID": "CVE-2020-0004",
	        "PkgName": "unknown-pkg",
	        "InstalledVersion": "1.0.0",
	        "FixedVersion": "1.0.1"
	      }
	    ]
	  }
	]`)

	sr := &models.ScanResult{
		JSONVersion: 2,
		ServerUUID:  "uuid",
	}
	res, err := Parse(vulnJSON, sr)
	if err != nil {
		t.Fatalf("unexpected error: %s", err)
	}
	if res != sr {
		t.Errorf("expected the supplied scan result to be returned")
	}
	if res.JSONVersion != 2 || res.ServerUUID != "uuid" {
		t.Errorf("caller-supplied fields not preserved: %#v", res)
	}

	if len(res.ScannedCves) != 4 {
		t.Fatalf("expected 4 vulns, got %d: %#v", len(res.ScannedCves), res.ScannedCves)
	}

	cve := res.ScannedCves["CVE-2020-0001"]
	if cve.CveID != "CVE-2020-0001" {
		t.Errorf("unexpected CveID: %s", cve.CveID)
	}
	if len(cve.Confidences) != 1 || cve.Confidences[0].Score != 100 ||
		cve.Confidences[0].DetectionMethod != "TrivyMatch" {
		t.Errorf("unexpected confidences: %#v", cve.Confidences)
	}
	if len(cve.AffectedPackages) != 3 {
		t.Errorf("expected merged affected packages, got %#v", cve.AffectedPackages)
	}
	content, ok := cve.CveContents[models.Trivy]
	if !ok {
		t.Fatalf("expected trivy cve content")
	}
	if content.Title != "lib title" || content.Summary != "lib description" || content.Cvss3Severity != "HIGH" {
		t.Errorf("unexpected cve content: %#v", content)
	}
	if len(content.References) != 2 ||
		content.References[0].Link != "https://a.example.com" ||
		content.References[0].Source != "trivy" ||
		content.References[1].Link != "https://b.example.com" {
		t.Errorf("unexpected references: %#v", content.References)
	}
	if len(cve.LibraryFixedIns) != 3 {
		t.Errorf("expected library fixed ins, got %#v", cve.LibraryFixedIns)
	}
	for _, lfi := range cve.LibraryFixedIns {
		if lfi.Key != "npm" {
			t.Errorf("unexpected library key: %s", lfi.Key)
		}
	}

	notFixed := res.ScannedCves["RUSTSEC-2020-0002"]
	if len(notFixed.AffectedPackages) != 1 {
		t.Fatalf("expected one affected package, got %#v", notFixed.AffectedPackages)
	}
	if !notFixed.AffectedPackages[0].NotFixedYet || notFixed.AffectedPackages[0].FixState != "Affected" {
		t.Errorf("expected not fixed yet: %#v", notFixed.AffectedPackages[0])
	}
	if len(notFixed.LibraryFixedIns) != 1 || notFixed.LibraryFixedIns[0].FixedIn != "" {
		t.Errorf("unexpected library fixed ins: %#v", notFixed.LibraryFixedIns)
	}

	if len(res.Packages) != 1 {
		t.Fatalf("expected 1 package, got %#v", res.Packages)
	}
	pkg, ok := res.Packages["os-pkg"]
	if !ok {
		t.Fatalf("expected os-pkg in packages")
	}
	if pkg.Name != "os-pkg" || pkg.Version != "3.0-1" || pkg.Release != "" {
		t.Errorf("unexpected package: %#v", pkg)
	}

	osCve := res.ScannedCves["CVE-2020-0003"]
	if len(osCve.LibraryFixedIns) != 0 {
		t.Errorf("OS findings must not have library fixed ins: %#v", osCve.LibraryFixedIns)
	}
	if fixed := osCve.AffectedPackages[0]; fixed.NotFixedYet || fixed.FixState != "" || fixed.FixedIn != "3.0-2" {
		t.Errorf("unexpected affected package: %#v", fixed)
	}

	if res.Family != "debian" || res.ServerName != "debian:10" {
		t.Errorf("unexpected family/server name: %s / %s", res.Family, res.ServerName)
	}
	if res.ScannedBy != "trivy" || res.ScannedVia != "trivy" {
		t.Errorf("unexpected scanned by/via: %s / %s", res.ScannedBy, res.ScannedVia)
	}
	if len(res.Optional) != 1 || res.Optional["trivy-target"] != "debian:10" {
		t.Errorf("unexpected optional: %#v", res.Optional)
	}

	if len(res.LibraryScanners) != 2 {
		t.Fatalf("expected 2 library scanners, got %#v", res.LibraryScanners)
	}
	if res.LibraryScanners[0].Path != "library" || res.LibraryScanners[1].Path != "unknown-target" {
		t.Errorf("scanners not sorted by path: %#v", res.LibraryScanners)
	}
	libs := res.LibraryScanners[0].Libs
	expected := []types.Library{
		{Name: "another-lib", Version: "2.0.0"},
		{Name: "bad-lib", Version: "1.0.0"},
		{Name: "rust-lib", Version: "0.1.0"},
	}
	if len(libs) != len(expected) {
		t.Fatalf("expected %d libs, got %#v", len(expected), libs)
	}
	for i, e := range expected {
		if libs[i] != e {
			t.Errorf("unexpected lib at %d: %#v, want %#v", i, libs[i], e)
		}
	}
}
