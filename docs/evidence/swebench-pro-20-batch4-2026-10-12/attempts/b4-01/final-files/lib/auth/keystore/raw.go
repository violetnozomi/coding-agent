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

package keystore

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"

	"github.com/gravitational/teleport"
	"github.com/gravitational/teleport/api/types"
	"github.com/gravitational/teleport/lib/sshutils"
	"github.com/gravitational/teleport/lib/utils"

	"github.com/gravitational/trace"
	"golang.org/x/crypto/ssh"
)

// RawConfig is the configuration for a rawKeyStore.
type RawConfig struct {
	// RSAKeyPairSource is a function that returns a new RSA key pair. It takes
	// a passphrase and returns PEM-encoded private key bytes, public key bytes
	// and an error. lib/auth/native.GenerateKeyPair satisfies this signature.
	RSAKeyPairSource func(passphrase string) (privKey []byte, pubKey []byte, err error)
}

// CheckAndSetDefaults validates the config and fills in defaults if needed.
func (c *RawConfig) CheckAndSetDefaults() error {
	if c.RSAKeyPairSource == nil {
		c.RSAKeyPairSource = generateRSAKeyPair
	}
	return nil
}

// NewRawKeyStore returns a new KeyStore that keeps keys in raw PEM-encoded
// form.
func NewRawKeyStore(config *RawConfig) KeyStore {
	if config == nil {
		config = &RawConfig{}
	}
	if err := config.CheckAndSetDefaults(); err != nil {
		// CheckAndSetDefaults never fails for this config, but keep the
		// instance usable regardless so that construction never yields nil.
		return &rawKeyStore{config: &RawConfig{RSAKeyPairSource: generateRSAKeyPair}}
	}
	return &rawKeyStore{
		config: config,
	}
}

type rawKeyStore struct {
	config *RawConfig
}

// GenerateRSA creates a new RSA key pair and returns the PEM-encoded private
// key as the key identifier, together with a signer for it.
func (s *rawKeyStore) GenerateRSA() (keyID []byte, signer crypto.Signer, err error) {
	priv, _, err := s.config.RSAKeyPairSource("")
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	signer, err = utils.ParsePrivateKey(priv)
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	// The raw key store uses the PEM-encoded private key as the key
	// identifier, so it can be plugged directly into a certificate authority
	// key pair.
	return priv, signer, nil
}

// GetSigner returns a signer for the given PEM-encoded key identifier.
func (s *rawKeyStore) GetSigner(keyID []byte) (crypto.Signer, error) {
	signer, err := utils.ParsePrivateKey(keyID)
	if err != nil {
		return nil, trace.Wrap(err)
	}
	return signer, nil
}

// DeleteKey is a no-op for the raw key store, since the store does not own the
// keys it hands out.
func (s *rawKeyStore) DeleteKey(keyID []byte) error {
	return nil
}

// GetTLSCertAndSigner returns the TLS certificate bytes and signer of the
// first RAW TLS key pair in the certificate authority.
func (s *rawKeyStore) GetTLSCertAndSigner(ca types.CertAuthority) ([]byte, crypto.Signer, error) {
	pairs := ca.GetActiveKeys().TLS
	for _, pair := range pairs {
		if isPKCS11(pair.KeyType, pair.Key) {
			continue
		}
		signer, err := utils.ParsePrivateKey(pair.Key)
		if err != nil {
			return nil, nil, trace.Wrap(err)
		}
		return pair.Cert, signer, nil
	}
	return nil, nil, trace.NotFound("no raw TLS private key found for certificate authority %q", ca.GetClusterName())
}

// GetSSHSigner returns an SSH signer for the first RAW SSH key pair in the
// certificate authority.
func (s *rawKeyStore) GetSSHSigner(ca types.CertAuthority) (ssh.Signer, error) {
	pairs := ca.GetActiveKeys().SSH
	for _, pair := range pairs {
		if isPKCS11(pair.PrivateKeyType, pair.PrivateKey) {
			continue
		}
		signer, err := ssh.ParsePrivateKey(pair.PrivateKey)
		if err != nil {
			return nil, trace.Wrap(err)
		}
		return sshutils.AlgSigner(signer, sshutils.GetSigningAlgName(ca)), nil
	}
	return nil, trace.NotFound("no raw SSH private key found for certificate authority %q", ca.GetClusterName())
}

// GetJWTSigner returns a signer for the first RAW JWT key pair in the
// certificate authority.
func (s *rawKeyStore) GetJWTSigner(ca types.CertAuthority) (crypto.Signer, error) {
	pairs := ca.GetActiveKeys().JWT
	for _, pair := range pairs {
		if isPKCS11(pair.PrivateKeyType, pair.PrivateKey) {
			continue
		}
		signer, err := utils.ParsePrivateKey(pair.PrivateKey)
		if err != nil {
			return nil, trace.Wrap(err)
		}
		return signer, nil
	}
	return nil, trace.NotFound("no raw JWT private key found for certificate authority %q", ca.GetClusterName())
}

// isPKCS11 reports whether a key pair holds a PKCS11-backed key, either
// explicitly marked as such or by inspecting the key bytes.
func isPKCS11(keyType types.PrivateKeyType, key []byte) bool {
	if keyType == types.PrivateKeyType_PKCS11 {
		return true
	}
	return KeyType(key) == types.PrivateKeyType_PKCS11
}

// generateRSAKeyPair is the default RSA key pair source, used when none is
// supplied in the config.
func generateRSAKeyPair(passphrase string) ([]byte, []byte, error) {
	priv, err := rsa.GenerateKey(rand.Reader, teleport.RSAKeySize)
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	privPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(priv),
	})
	pub, err := ssh.NewPublicKey(&priv.PublicKey)
	if err != nil {
		return nil, nil, trace.Wrap(err)
	}
	return privPEM, ssh.MarshalAuthorizedKey(pub), nil
}
