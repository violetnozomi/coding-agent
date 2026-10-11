package config

import (
	"errors"
	"fmt"
)

const (
	fieldErrFmt    = "field %q: %w"
	providerErrFmt = "provider %q: %w"
)

var (
	// errValidationRequired is returned when a required value is
	// either not supplied or supplied with empty value.
	errValidationRequired = errors.New("non-empty value is required")
	// errPositiveNonZeroDuration is returned when a negative or zero time.Duration is provided.
	errPositiveNonZeroDuration = errors.New("positive non-zero duration required")
	// errReadOrgScopeRequired is returned when allowed_organizations is configured
	// but the associated read:org scope has not been supplied.
	errReadOrgScopeRequired = errors.New("must contain read:org when allowed_organizations is not empty")
)

func errFieldWrap(field string, err error) error {
	return fmt.Errorf(fieldErrFmt, field, err)
}

func errFieldRequired(field string) error {
	return errFieldWrap(field, errValidationRequired)
}

func errProviderWrap(provider string, err error) error {
	return fmt.Errorf(providerErrFmt, provider, err)
}
