/*
Copyright 2020 Gravitational, Inc.

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

package main

import (
	"context"
	"fmt"
	"time"

	"github.com/gravitational/kingpin"
	"github.com/gravitational/teleport/lib/asciitable"
	"github.com/gravitational/teleport/lib/client"
	"github.com/gravitational/teleport/lib/kube/kubeconfig"
	kubeutils "github.com/gravitational/teleport/lib/kube/utils"
	"github.com/gravitational/teleport/lib/utils"
	"github.com/gravitational/trace"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/client-go/pkg/apis/clientauthentication"
	clientauthv1beta1 "k8s.io/client-go/pkg/apis/clientauthentication/v1beta1"
)

type kubeCommands struct {
	credentials *kubeCredentialsCommand
	ls          *kubeLSCommand
	login       *kubeLoginCommand
}

func newKubeCommand(app *kingpin.Application) kubeCommands {
	kube := app.Command("kube", "Manage available kubernetes clusters")
	cmds := kubeCommands{
		credentials: newKubeCredentialsCommand(kube),
		ls:          newKubeLSCommand(kube),
		login:       newKubeLoginCommand(kube),
	}
	return cmds
}

type kubeCredentialsCommand struct {
	*kingpin.CmdClause
	kubeCluster     string
	teleportCluster string
}

func newKubeCredentialsCommand(parent *kingpin.CmdClause) *kubeCredentialsCommand {
	c := &kubeCredentialsCommand{
		// This command is always hidden. It's called from the kubeconfig that
		// tsh generates and never by users directly.
		CmdClause: parent.Command("credentials", "Get credentials for kubectl access").Hidden(),
	}
	c.Flag("teleport-cluster", "Name of the teleport cluster to get credentials for.").Required().StringVar(&c.teleportCluster)
	c.Flag("kube-cluster", "Name of the kubernetes cluster to get credentials for.").Required().StringVar(&c.kubeCluster)
	return c
}

func (c *kubeCredentialsCommand) run(cf *CLIConf) error {
	tc, err := makeClient(cf, true)
	if err != nil {
		return trace.Wrap(err)
	}

	// Try loading existing keys.
	k, err := tc.LocalAgent().GetKey(c.teleportCluster, client.WithKubeCerts{})
	if err != nil && !trace.IsNotFound(err) {
		return trace.Wrap(err)
	}
	// Loaded existing credentials and have a cert for this cluster? Return it
	// right away.
	if err == nil {
		crt, err := k.KubeTLSCertificate(c.kubeCluster)
		if err != nil && !trace.IsNotFound(err) {
			return trace.Wrap(err)
		}
		if crt != nil && time.Until(crt.NotAfter) > time.Minute {
			log.Debugf("Re-using existing TLS cert for kubernetes cluster %q", c.kubeCluster)
			return c.writeResponse(k, c.kubeCluster)
		}
		// Otherwise, cert for this k8s cluster is missing or expired. Request
		// a new one.
	}

	log.Debugf("Requesting TLS cert for kubernetes cluster %q", c.kubeCluster)
	err = client.RetryWithRelogin(cf.Context, tc, func() error {
		var err error
		k, err = tc.IssueUserCertsWithMFA(cf.Context, client.ReissueParams{
			RouteToCluster:    c.teleportCluster,
			KubernetesCluster: c.kubeCluster,
		})
		return err
	})
	if err != nil {
		return trace.Wrap(err)
	}

	// Cache the new cert on disk for reuse.
	if _, err := tc.LocalAgent().AddKey(k); err != nil {
		return trace.Wrap(err)
	}

	return c.writeResponse(k, c.kubeCluster)
}

func (c *kubeCredentialsCommand) writeResponse(key *client.Key, kubeClusterName string) error {
	crt, err := key.KubeTLSCertificate(kubeClusterName)
	if err != nil {
		return trace.Wrap(err)
	}
	expiry := crt.NotAfter
	// Indicate slightly earlier expiration to avoid the cert expiring
	// mid-request, if possible.
	if time.Until(expiry) > time.Minute {
		expiry = expiry.Add(-1 * time.Minute)
	}
	resp := &clientauthentication.ExecCredential{
		Status: &clientauthentication.ExecCredentialStatus{
			ExpirationTimestamp:   &metav1.Time{Time: expiry},
			ClientCertificateData: string(key.KubeTLSCerts[kubeClusterName]),
			ClientKeyData:         string(key.Priv),
		},
	}
	data, err := runtime.Encode(kubeCodecs.LegacyCodec(kubeGroupVersion), resp)
	if err != nil {
		return trace.Wrap(err)
	}
	fmt.Println(string(data))
	return nil
}

type kubeLSCommand struct {
	*kingpin.CmdClause
}

func newKubeLSCommand(parent *kingpin.CmdClause) *kubeLSCommand {
	c := &kubeLSCommand{
		CmdClause: parent.Command("ls", "Get a list of kubernetes clusters"),
	}
	return c
}

func (c *kubeLSCommand) run(cf *CLIConf) error {
	tc, err := makeClient(cf, true)
	if err != nil {
		return trace.Wrap(err)
	}
	currentTeleportCluster, kubeClusters, err := fetchKubeClusters(cf.Context, tc)
	if err != nil {
		return trace.Wrap(err)
	}

	var selectedCluster string
	if kc, err := kubeconfig.Load(""); err != nil {
		log.WithError(err).Warning("Failed parsing existing kubeconfig")
	} else {
		selectedCluster = kubeconfig.KubeClusterFromContext(kc.CurrentContext, currentTeleportCluster)
	}

	var t asciitable.Table
	if cf.Quiet {
		t = asciitable.MakeHeadlessTable(2)
	} else {
		t = asciitable.MakeTable([]string{"Kube Cluster Name", "Selected"})
	}
	for _, cluster := range kubeClusters {
		var selectedMark string
		if cluster == selectedCluster {
			selectedMark = "*"
		}
		t.AddRow([]string{cluster, selectedMark})
	}
	fmt.Println(t.AsBuffer().String())

	return nil
}

type kubeLoginCommand struct {
	*kingpin.CmdClause
	kubeCluster string
}

func newKubeLoginCommand(parent *kingpin.CmdClause) *kubeLoginCommand {
	c := &kubeLoginCommand{
		CmdClause: parent.Command("login", "Login to a kubernetes cluster"),
	}
	c.Arg("kube-cluster", "Name of the kubernetes cluster to login to. Check 'tsh kube ls' for a list of available clusters.").Required().StringVar(&c.kubeCluster)
	return c
}

func (c *kubeLoginCommand) run(cf *CLIConf) error {
	tc, err := makeClient(cf, true)
	if err != nil {
		return trace.Wrap(err)
	}
	// Check that this kube cluster exists.
	_, kubeClusters, err := fetchKubeClusters(cf.Context, tc)
	if err != nil {
		return trace.Wrap(err)
	}
	if !utils.SliceContainsStr(kubeClusters, c.kubeCluster) {
		return trace.NotFound("kubernetes cluster %q not found, check 'tsh kube ls' for a list of known clusters", c.kubeCluster)
	}

	// Setting cf.KubernetesCluster makes buildKubeConfigUpdate select this
	// cluster in the kubeconfig. This also re-generates kubeconfig contexts for
	// any clusters added after the last 'tsh login'.
	cf.KubernetesCluster = c.kubeCluster
	if err := updateKubeConfig(cf, tc); err != nil {
		return trace.Wrap(err)
	}

	fmt.Printf("Logged into kubernetes cluster %q\n", c.kubeCluster)
	return nil
}

func fetchKubeClusters(ctx context.Context, tc *client.TeleportClient) (teleportCluster string, kubeClusters []string, err error) {
	err = client.RetryWithRelogin(ctx, tc, func() error {
		pc, err := tc.ConnectToProxy(ctx)
		if err != nil {
			return trace.Wrap(err)
		}
		defer pc.Close()
		ac, err := pc.ConnectToCurrentCluster(ctx, true)
		if err != nil {
			return trace.Wrap(err)
		}
		defer ac.Close()

		cn, err := ac.GetClusterName()
		if err != nil {
			return trace.Wrap(err)
		}
		teleportCluster = cn.GetClusterName()

		kubeClusters, err = kubeutils.KubeClusterNames(ctx, ac)
		if err != nil {
			return trace.Wrap(err)
		}
		return nil
	})
	if err != nil {
		return "", nil, trace.Wrap(err)
	}
	return teleportCluster, kubeClusters, nil
}

// kubernetesStatus holds teleport client information necessary to populate a
// kubeconfig for the current user.
type kubernetesStatus struct {
	// clusterAddr is the public address of the teleport cluster the kubernetes
	// client will talk to, usually a proxy.
	clusterAddr string
	// teleportClusterName is the name of the teleport cluster used to name the
	// generated kubeconfig sections.
	teleportClusterName string
	// kubeClusters is a list of kubernetes clusters registered in the teleport
	// cluster.
	kubeClusters []string
	// credentials are the user credentials used for authentication to
	// clusterAddr.
	credentials *client.Key
}

// fetchKubeStatus returns the current kubernetes status for the logged in
// user, or nil if the teleport cluster does not advertise Kubernetes support.
func fetchKubeStatus(ctx context.Context, tc *client.TeleportClient) (*kubernetesStatus, error) {
	// Fetch proxy's advertised ports to check for k8s support.
	if _, err := tc.Ping(ctx); err != nil {
		return nil, trace.Wrap(err)
	}
	if tc.KubeProxyAddr == "" {
		// Kubernetes support disabled, don't touch kubeconfig.
		return nil, nil
	}

	kubeStatus := &kubernetesStatus{
		clusterAddr: tc.KubeClusterAddr(),
	}
	kubeStatus.teleportClusterName, _ = tc.KubeProxyHostPort()
	if tc.SiteName != "" {
		kubeStatus.teleportClusterName = tc.SiteName
	}

	var err error
	kubeStatus.credentials, err = tc.LocalAgent().GetCoreKey()
	if err != nil {
		return nil, trace.Wrap(err)
	}

	// Fetch the list of known kubernetes clusters from the proxy.
	pc, err := tc.ConnectToProxy(ctx)
	if err != nil {
		return nil, trace.Wrap(err)
	}
	defer pc.Close()
	ac, err := pc.ConnectToCurrentCluster(ctx, true)
	if err != nil {
		return nil, trace.Wrap(err)
	}
	defer ac.Close()
	kubeStatus.kubeClusters, err = kubeutils.KubeClusterNames(ctx, ac)
	if err != nil && !trace.IsNotFound(err) {
		return nil, trace.Wrap(err)
	}

	return kubeStatus, nil
}

// buildKubeConfigUpdate returns kubeconfig.Values suitable for updating the
// user's kubeconfig based on the CLI configuration and the user's kubernetes
// status.
//
// The current kubeconfig context is only changed when the user explicitly
// selected a kubernetes cluster with --kube-cluster. Otherwise, the exec plugin
// is configured with the list of known clusters but no cluster is selected, so
// the current kubectl context is preserved.
func buildKubeConfigUpdate(cf *CLIConf, kubeStatus *kubernetesStatus) (*kubeconfig.Values, error) {
	values := &kubeconfig.Values{
		ClusterAddr:         kubeStatus.clusterAddr,
		TeleportClusterName: kubeStatus.teleportClusterName,
		Credentials:         kubeStatus.credentials,
	}

	// Without a path to the tsh binary or any kubernetes clusters, fall back to
	// static credentials and leave the exec plugin unset.
	if cf.executablePath == "" || len(kubeStatus.kubeClusters) == 0 {
		return values, nil
	}

	values.Exec = &kubeconfig.ExecValues{
		TshBinaryPath:     cf.executablePath,
		TshBinaryInsecure: cf.InsecureSkipVerify,
		KubeClusters:      kubeStatus.kubeClusters,
	}

	// Only switch the current kubeconfig context when the user explicitly asked
	// for a specific kubernetes cluster.
	if cf.KubernetesCluster == "" {
		return values, nil
	}
	if !utils.SliceContainsStr(kubeStatus.kubeClusters, cf.KubernetesCluster) {
		return nil, trace.BadParameter("kubernetes cluster %q not found, check 'tsh kube ls' for a list of known clusters", cf.KubernetesCluster)
	}
	values.Exec.SelectCluster = cf.KubernetesCluster

	return values, nil
}

// updateKubeConfig refreshes the user's kubeconfig with the current teleport
// kubernetes status. If the teleport cluster does not advertise Kubernetes
// support, kubeconfig is left untouched.
//
// By default the active kubectl context is preserved; it is only changed when
// the user explicitly selected a cluster with --kube-cluster.
func updateKubeConfig(cf *CLIConf, tc *client.TeleportClient) error {
	kubeStatus, err := fetchKubeStatus(cf.Context, tc)
	if err != nil {
		return trace.Wrap(err)
	}
	if kubeStatus == nil {
		return nil
	}
	values, err := buildKubeConfigUpdate(cf, kubeStatus)
	if err != nil {
		return trace.Wrap(err)
	}
	return trace.Wrap(kubeconfig.Update("", *values))
}

// Required magic boilerplate to use the k8s encoder.

var (
	kubeScheme       = runtime.NewScheme()
	kubeCodecs       = serializer.NewCodecFactory(kubeScheme)
	kubeGroupVersion = schema.GroupVersion{
		Group:   "client.authentication.k8s.io",
		Version: "v1beta1",
	}
)

func init() {
	metav1.AddToGroupVersion(kubeScheme, schema.GroupVersion{Version: "v1"})
	clientauthv1beta1.AddToScheme(kubeScheme)
	clientauthentication.AddToScheme(kubeScheme)
}
