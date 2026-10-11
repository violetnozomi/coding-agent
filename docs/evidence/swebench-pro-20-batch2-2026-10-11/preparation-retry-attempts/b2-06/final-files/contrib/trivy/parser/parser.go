package parser

import (
	"encoding/json"
	"sort"

	fanalos "github.com/aquasecurity/fanal/analyzer/os"
	"github.com/aquasecurity/trivy/pkg/types"
	"github.com/future-architect/vuls/models"
	"golang.org/x/xerrors"
)

// result is a single Trivy scan result.
type result struct {
	Target          string                        `json:"Target"`
	Type            string                        `json:"Type"`
	Vulnerabilities []types.DetectedVulnerability `json:"Vulnerabilities"`
}

// results is a list of Trivy scan results.
type results []result

// IsTrivySupportedOS returns true if the given Trivy result type corresponds to
// a supported operating system family.
func IsTrivySupportedOS(family string) bool {
	switch family {
	case fanalos.Alpine, fanalos.Debian, fanalos.Ubuntu, fanalos.CentOS,
		fanalos.RedHat, fanalos.Fedora, fanalos.Amazon, fanalos.Oracle,
		fanalos.Photon, fanalos.OpenSUSE, fanalos.OpenSUSELeap,
		fanalos.OpenSUSETumbleweed, fanalos.SLES, fanalos.Windows:
		return true
	default:
		return false
	}
}

// Parse parses a Trivy JSON report and populates the given scan result.
// It returns the populated scan result, or a nil result and an error if the
// JSON could not be unmarshalled.
func Parse(vulnJSON []byte, scanResult *models.ScanResult) (*models.ScanResult, error) {
	var rs results
	if err := json.Unmarshal(vulnJSON, &rs); err != nil {
		return nil, xerrors.Errorf("failed to unmarshal Trivy JSON: %w", err)
	}

	if scanResult == nil {
		scanResult = &models.ScanResult{}
	}

	scannedCves := models.VulnInfos{}
	packages := models.Packages{}
	libraryScannerMap := map[string][]types.Library{}

	for _, r := range rs {
		isOSFamily := IsTrivySupportedOS(r.Type)
		for _, vuln := range r.Vulnerabilities {
			vinfo, found := scannedCves[vuln.VulnerabilityID]
			if !found {
				vinfo = models.VulnInfo{
					CveID:       vuln.VulnerabilityID,
					Confidences: models.Confidences{models.TrivyMatch},
				}
			}

			pack := models.PackageFixStatus{
				Name:    vuln.PkgName,
				FixedIn: vuln.FixedVersion,
			}
			if vuln.FixedVersion == "" {
				pack.NotFixedYet = true
				pack.FixState = "Affected"
			}
			vinfo.AffectedPackages = append(vinfo.AffectedPackages, pack)

			refs := models.References{}
			for _, ref := range vuln.References {
				refs = append(refs, models.Reference{Source: "trivy", Link: ref})
			}
			sort.Slice(refs, func(i, j int) bool {
				return refs[i].Link < refs[j].Link
			})

			// Keep already collected content when merging the same
			// vulnerability identifier seen in multiple results.
			if existing, ok := vinfo.CveContents[models.Trivy]; !ok || existing.Empty() {
				vinfo.CveContents = models.CveContents{
					models.Trivy: {
						Title:         vuln.Title,
						Summary:       vuln.Description,
						Cvss3Severity: vuln.Severity,
						References:    refs,
					},
				}
			}

			if isOSFamily {
				packages[vuln.PkgName] = models.Package{
					Name:    vuln.PkgName,
					Version: vuln.InstalledVersion,
				}
				scanResult.Family = r.Type
				scanResult.ServerName = r.Target
				scanResult.ScannedBy = "trivy"
				scanResult.ScannedVia = "trivy"
				scanResult.Optional = map[string]interface{}{
					"trivy-target": r.Target,
				}
			} else {
				vinfo.LibraryFixedIns = append(vinfo.LibraryFixedIns, models.LibraryFixedIn{
					Key:     r.Type,
					Name:    vuln.PkgName,
					FixedIn: vuln.FixedVersion,
				})
				libraryScannerMap[r.Target] = append(libraryScannerMap[r.Target], types.Library{
					Name:    vuln.PkgName,
					Version: vuln.InstalledVersion,
				})
			}

			scannedCves[vuln.VulnerabilityID] = vinfo
		}
	}

	libraryScanners := models.LibraryScanners{}
	for target, libs := range libraryScannerMap {
		uniq := map[string]types.Library{}
		for _, lib := range libs {
			uniq[lib.Name+"@"+lib.Version] = lib
		}
		scanner := models.LibraryScanner{Path: target}
		for _, lib := range uniq {
			scanner.Libs = append(scanner.Libs, lib)
		}
		sort.Slice(scanner.Libs, func(i, j int) bool {
			return scanner.Libs[i].Name < scanner.Libs[j].Name
		})
		libraryScanners = append(libraryScanners, scanner)
	}
	sort.Slice(libraryScanners, func(i, j int) bool {
		return libraryScanners[i].Path < libraryScanners[j].Path
	})

	scanResult.ScannedCves = scannedCves
	scanResult.Packages = packages
	scanResult.LibraryScanners = libraryScanners

	return scanResult, nil
}
