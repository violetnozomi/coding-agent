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

// Package keystore provides an abstraction over cryptographic key storage and
// signing operations used by the auth service.
//
// Consumers only interact with the KeyStore interface, so alternative key
// backends (for example an HSM or a cloud key manager) can be plugged in
// without changing the code that needs to sign SSH, TLS or JWT material.
package keystore

import (
	"bytes"
	"crypto"

	"github.com/gravitational/teleport/api/types"

	"golang.org/x/crypto/ssh"
)

// pkcs11Prefix is the literal prefix that marks a private key as being backed
// by a PKCS#11 device (such as an HSM) rather than held in raw PEM form.
const pkcs11Prefix = "pkcs11:"

// KeyStore is a generic storage for cryptographic keys.
//
// Keys are addressed by an opaque identifier returned from GenerateRSA. The
// same identifier can later be passed back to GetSigner to obtain an
// equivalent signer.
type KeyStore interface {
	// GenerateRSA creates a new RSA private key and returns its identifier
	// together with a signer for it. The identifier can later be passed to
	// GetSigner to get a signer for the same key.
	GenerateRSA() (keyID []byte, signer crypto.Signer, err error)

	// GetSigner returns a signer for the given key identifier.
	GetSigner(keyID []byte) (crypto.Signer, error)

	// GetTLSCertAndSigner returns the raw certificate bytes and signer that
	// should be used for TLS by the given certificate authority. An error is
	// returned when no usable TLS key pair is found.
	GetTLSCertAndSigner(ca types.CertAuthority) ([]byte, crypto.Signer, error)

	// GetSSHSigner returns the SSH signer that should be used by the given
	// certificate authority. An error is returned when no usable SSH key pair
	// is found.
	GetSSHSigner(ca types.CertAuthority) (ssh.Signer, error)

	// GetJWTSigner returns the signer that should be used for JWT signing by
	// the given certificate authority. An error is returned when no usable JWT
	// key pair is found.
	GetJWTSigner(ca types.CertAuthority) (crypto.Signer, error)

	// DeleteKey deletes the key with the given identifier from the store.
	DeleteKey(keyID []byte) error
}

// KeyType returns the type of the given private key.
//
// A key is classified as PKCS11 if and only if it begins with the literal
// prefix "pkcs11:". Any other content is classified as RAW.
func KeyType(key []byte) types.PrivateKeyType {
	if bytes.HasPrefix(key, []byte(pkcs11Prefix)) {
		return types.PrivateKeyType_PKCS11
	}
	return types.PrivateKeyType_RAW
}
