package controller

import (
	"context"
	"fmt"
	"net"
	"reflect"
	"sort"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/intstr"
	utilvalidation "k8s.io/apimachinery/pkg/util/validation"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	routerv1alpha1 "github.com/example/mysql-router-operator/api/v1alpha1"
)

const managedByLabelValue = "mysqlrouter-operator.router.example.com"

type MySQLRouterReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

func (r *MySQLRouterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var router routerv1alpha1.MySQLRouter
	if err := r.Get(ctx, req.NamespacedName, &router); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	desired := router.DeepCopy()
	desired.Default()

	if err := validateMySQLRouter(desired); err != nil {
		logger.Error(err, "invalid MySQLRouter spec")
		if statusErr := r.updateStatus(ctx, desired, nil, nil, nil, err); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{}, nil
	}

	bootstrapNode, err := bootstrapNodeFor(desired)
	if err != nil {
		if statusErr := r.updateStatus(ctx, desired, nil, nil, nil, err); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{}, nil
	}

	nodeServiceNames := make([]string, 0, len(desired.Spec.ExternalNodes))
	endpointSliceNames := make([]string, 0, len(desired.Spec.ExternalNodes))

	for _, node := range desired.Spec.ExternalNodes {
		svcName := externalNodeServiceName(desired.Name, node.Name)
		epsName := externalNodeEndpointSliceName(svcName)

		if err := r.reconcileExternalNodeService(ctx, desired, node); err != nil {
			if statusErr := r.updateStatus(ctx, desired, nil, nodeServiceNames, endpointSliceNames, err); statusErr != nil {
				return ctrl.Result{}, statusErr
			}
			return ctrl.Result{}, err
		}

		if err := r.reconcileExternalNodeEndpointSlice(ctx, desired, node); err != nil {
			if statusErr := r.updateStatus(ctx, desired, nil, nodeServiceNames, endpointSliceNames, err); statusErr != nil {
				return ctrl.Result{}, statusErr
			}
			return ctrl.Result{}, err
		}

		nodeServiceNames = append(nodeServiceNames, svcName)
		endpointSliceNames = append(endpointSliceNames, epsName)
	}

	if err := r.reconcileRouterService(ctx, desired); err != nil {
		if statusErr := r.updateStatus(ctx, desired, nil, nodeServiceNames, endpointSliceNames, err); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{}, err
	}

	deployment, err := r.reconcileRouterDeployment(ctx, desired, bootstrapNode)
	if err != nil {
		if statusErr := r.updateStatus(ctx, desired, nil, nodeServiceNames, endpointSliceNames, err); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{}, err
	}

	if err := r.updateStatus(ctx, desired, deployment, nodeServiceNames, endpointSliceNames, nil); err != nil {
		return ctrl.Result{}, err
	}

	return ctrl.Result{}, nil
}

func (r *MySQLRouterReconciler) reconcileExternalNodeService(ctx context.Context, router *routerv1alpha1.MySQLRouter, node routerv1alpha1.ExternalNode) error {
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      externalNodeServiceName(router.Name, node.Name),
			Namespace: router.Namespace,
		},
	}

	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, svc, func() error {
		if err := controllerutil.SetControllerReference(router, svc, r.Scheme); err != nil {
			return err
		}
		labels := labelsForRouter(router)
		labels["mysqlrouter.router.example.com/external-node"] = node.Name
		svc.Labels = labels
		svc.Spec.Selector = nil
		svc.Spec.Type = corev1.ServiceTypeClusterIP
		svc.Spec.Ports = []corev1.ServicePort{{
			Name:     "mysql",
			Port:     node.Port,
			Protocol: corev1.ProtocolTCP,
		}}
		return nil
	})
	return err
}

func (r *MySQLRouterReconciler) reconcileExternalNodeEndpointSlice(ctx context.Context, router *routerv1alpha1.MySQLRouter, node routerv1alpha1.ExternalNode) error {
	svcName := externalNodeServiceName(router.Name, node.Name)
	eps := &discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{
			Name:      externalNodeEndpointSliceName(svcName),
			Namespace: router.Namespace,
		},
	}

	addressType := discoveryv1.AddressTypeIPv4
	if parsed := net.ParseIP(node.Address); parsed != nil && parsed.To4() == nil {
		addressType = discoveryv1.AddressTypeIPv6
	}

	ready := true
	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, eps, func() error {
		if err := controllerutil.SetControllerReference(router, eps, r.Scheme); err != nil {
			return err
		}
		labels := labelsForRouter(router)
		labels["kubernetes.io/service-name"] = svcName
		labels["endpointslice.kubernetes.io/managed-by"] = managedByLabelValue
		labels["mysqlrouter.router.example.com/external-node"] = node.Name
		eps.Labels = labels
		eps.AddressType = addressType
		eps.Ports = []discoveryv1.EndpointPort{{
			Name:     ptrTo("mysql"),
			Port:     ptrTo(node.Port),
			Protocol: ptrTo(corev1.ProtocolTCP),
		}}
		eps.Endpoints = []discoveryv1.Endpoint{{
			Addresses: []string{node.Address},
			Conditions: discoveryv1.EndpointConditions{
				Ready: &ready,
			},
		}}
		return nil
	})
	return err
}

func (r *MySQLRouterReconciler) reconcileRouterService(ctx context.Context, router *routerv1alpha1.MySQLRouter) error {
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      router.Name,
			Namespace: router.Namespace,
		},
	}

	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, svc, func() error {
		if err := controllerutil.SetControllerReference(router, svc, r.Scheme); err != nil {
			return err
		}
		svc.Labels = labelsForRouter(router)
		svc.Annotations = router.Spec.ServiceAnnotations
		svc.Spec.Type = corev1.ServiceType(router.Spec.ServiceType)
		svc.Spec.Selector = selectorLabelsForRouter(router)
		svc.Spec.Ports = routerServicePorts(router)
		return nil
	})
	return err
}

func (r *MySQLRouterReconciler) reconcileRouterDeployment(ctx context.Context, router *routerv1alpha1.MySQLRouter, bootstrapNode routerv1alpha1.ExternalNode) (*appsv1.Deployment, error) {
	deploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      router.Name,
			Namespace: router.Namespace,
		},
	}

	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, deploy, func() error {
		if err := controllerutil.SetControllerReference(router, deploy, r.Scheme); err != nil {
			return err
		}

		deploy.Labels = labelsForRouter(router)
		deploy.Spec.Replicas = router.Spec.Replicas
		deploy.Spec.Selector = &metav1.LabelSelector{MatchLabels: selectorLabelsForRouter(router)}
		deploy.Spec.Template.ObjectMeta.Labels = selectorLabelsForRouter(router)

		volume, volumeMount := storageVolume(router)
		basePort := router.Spec.Routing.BasePort

		deploy.Spec.Template.Spec.Volumes = []corev1.Volume{volume}
		deploy.Spec.Template.Spec.Containers = []corev1.Container{{
			Name:            "mysqlrouter",
			Image:           router.Spec.Image,
			ImagePullPolicy: corev1.PullPolicy(router.Spec.ImagePullPolicy),
			Command:         []string{"/bin/sh", "-ec", bootstrapScript(router)},
			Env: []corev1.EnvVar{
				secretEnvVar("MYSQL_BOOTSTRAP_USER", router.Spec.Bootstrap.UsernameSecretRef),
				secretEnvVar("MYSQL_BOOTSTRAP_PASSWORD", router.Spec.Bootstrap.PasswordSecretRef),
				{Name: "MYSQL_BOOTSTRAP_HOST", Value: externalNodeServiceName(router.Name, bootstrapNode.Name)},
				{Name: "MYSQL_BOOTSTRAP_PORT", Value: fmt.Sprintf("%d", bootstrapNode.Port)},
				{Name: "MYSQL_ROUTER_BASE_PORT", Value: fmt.Sprintf("%d", basePort)},
				{Name: "MYSQL_ROUTER_BIND_ADDRESS", Value: router.Spec.Routing.BindAddress},
			},
			Ports:        routerContainerPorts(router),
			VolumeMounts: []corev1.VolumeMount{volumeMount},
			ReadinessProbe: &corev1.Probe{
				ProbeHandler: corev1.ProbeHandler{
					TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromInt32(basePort)},
				},
				InitialDelaySeconds: 10,
				PeriodSeconds:       5,
			},
			LivenessProbe: &corev1.Probe{
				ProbeHandler: corev1.ProbeHandler{
					TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromInt32(basePort)},
				},
				InitialDelaySeconds: 30,
				PeriodSeconds:       10,
			},
		}}
		return nil
	})
	if err != nil {
		return nil, err
	}

	current := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(deploy), current); err != nil {
		return nil, err
	}
	return current, nil
}

func (r *MySQLRouterReconciler) updateStatus(
	ctx context.Context,
	router *routerv1alpha1.MySQLRouter,
	deployment *appsv1.Deployment,
	nodeServiceNames []string,
	endpointSliceNames []string,
	reconcileErr error,
) error {
	current := &routerv1alpha1.MySQLRouter{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(router), current); err != nil {
		return err
	}

	desired := current.DeepCopy()
	desired.Status.ObservedGeneration = current.Generation
	desired.Status.DeploymentName = current.Name
	desired.Status.ServiceName = current.Name
	desired.Status.NodeServiceNames = sortedCopy(nodeServiceNames)
	desired.Status.EndpointSliceNames = sortedCopy(endpointSliceNames)

	if deployment != nil {
		desired.Status.ReadyReplicas = deployment.Status.ReadyReplicas
		desired.Status.Phase = derivePhase(deployment)
	} else if reconcileErr != nil {
		desired.Status.Phase = "Error"
	} else {
		desired.Status.Phase = "Pending"
	}

	if reconcileErr != nil {
		apimeta.SetStatusCondition(&desired.Status.Conditions, metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionFalse,
			Reason:             "ReconcileError",
			Message:            reconcileErr.Error(),
			ObservedGeneration: current.Generation,
		})
		apimeta.SetStatusCondition(&desired.Status.Conditions, metav1.Condition{
			Type:               "Reconciled",
			Status:             metav1.ConditionFalse,
			Reason:             "ReconcileError",
			Message:            reconcileErr.Error(),
			ObservedGeneration: current.Generation,
		})
	} else {
		readyStatus := metav1.ConditionFalse
		readyReason := "Progressing"
		readyMessage := "Deployment is reconciling"
		if deployment != nil && deployment.Status.ReadyReplicas >= valueOrDefault(current.Spec.Replicas, 1) {
			readyStatus = metav1.ConditionTrue
			readyReason = "Ready"
			readyMessage = "Deployment is ready"
		}

		apimeta.SetStatusCondition(&desired.Status.Conditions, metav1.Condition{
			Type:               "Ready",
			Status:             readyStatus,
			Reason:             readyReason,
			Message:            readyMessage,
			ObservedGeneration: current.Generation,
		})
		apimeta.SetStatusCondition(&desired.Status.Conditions, metav1.Condition{
			Type:               "Reconciled",
			Status:             metav1.ConditionTrue,
			Reason:             "Success",
			Message:            "Resources are in sync with spec",
			ObservedGeneration: current.Generation,
		})
	}

	if reflect.DeepEqual(current.Status, desired.Status) {
		return nil
	}
	current.Status = desired.Status
	return r.Status().Update(ctx, current)
}

func (r *MySQLRouterReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&routerv1alpha1.MySQLRouter{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&discoveryv1.EndpointSlice{}).
		Complete(r)
}

func validateMySQLRouter(router *routerv1alpha1.MySQLRouter) error {
	if len(router.Spec.ExternalNodes) == 0 {
		return fmt.Errorf("spec.externalNodes must contain at least one item")
	}
	if router.Spec.Routing.BasePort < 1 || router.Spec.Routing.BasePort > 65532 {
		return fmt.Errorf("spec.routing.basePort must be between 1 and 65532")
	}
	if router.Spec.Storage.Type == "PersistentVolumeClaim" && router.Spec.Storage.ClaimName == "" {
		return fmt.Errorf("spec.storage.claimName is required when spec.storage.type is PersistentVolumeClaim")
	}
	if router.Spec.Bootstrap.UsernameSecretRef.Name == "" || router.Spec.Bootstrap.UsernameSecretRef.Key == "" {
		return fmt.Errorf("spec.bootstrap.usernameSecretRef.name and key are required")
	}
	if router.Spec.Bootstrap.PasswordSecretRef.Name == "" || router.Spec.Bootstrap.PasswordSecretRef.Key == "" {
		return fmt.Errorf("spec.bootstrap.passwordSecretRef.name and key are required")
	}

	seen := map[string]struct{}{}
	for _, node := range router.Spec.ExternalNodes {
		if node.Name == "" {
			return fmt.Errorf("each external node must have a name")
		}
		if errs := utilvalidation.IsDNS1123Label(node.Name); len(errs) > 0 {
			return fmt.Errorf("external node %q is not a valid DNS-1123 label: %s", node.Name, strings.Join(errs, ", "))
		}
		if _, exists := seen[node.Name]; exists {
			return fmt.Errorf("duplicate external node name %q", node.Name)
		}
		seen[node.Name] = struct{}{}
		if net.ParseIP(node.Address) == nil {
			return fmt.Errorf("external node %q address %q must be an IPv4 or IPv6 address", node.Name, node.Address)
		}
		if node.Port < 1 || node.Port > 65535 {
			return fmt.Errorf("external node %q port must be between 1 and 65535", node.Name)
		}
	}
	return nil
}

func bootstrapNodeFor(router *routerv1alpha1.MySQLRouter) (routerv1alpha1.ExternalNode, error) {
	if router.Spec.Bootstrap.NodeName == "" {
		return router.Spec.ExternalNodes[0], nil
	}
	for _, node := range router.Spec.ExternalNodes {
		if node.Name == router.Spec.Bootstrap.NodeName {
			return node, nil
		}
	}
	return routerv1alpha1.ExternalNode{}, fmt.Errorf("spec.bootstrap.nodeName %q was not found in spec.externalNodes", router.Spec.Bootstrap.NodeName)
}

func labelsForRouter(router *routerv1alpha1.MySQLRouter) map[string]string {
	labels := selectorLabelsForRouter(router)
	labels["app.kubernetes.io/managed-by"] = managedByLabelValue
	labels["app.kubernetes.io/component"] = "mysql-router"
	return labels
}

func selectorLabelsForRouter(router *routerv1alpha1.MySQLRouter) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":     "mysql-router",
		"app.kubernetes.io/instance": router.Name,
	}
}

func externalNodeServiceName(routerName, nodeName string) string {
	return fmt.Sprintf("%s-%s", routerName, nodeName)
}

func externalNodeEndpointSliceName(serviceName string) string {
	return fmt.Sprintf("%s-manual", serviceName)
}

func routerServicePorts(router *routerv1alpha1.MySQLRouter) []corev1.ServicePort {
	base := router.Spec.Routing.BasePort
	ports := []corev1.ServicePort{
		{Name: "mysql-rw", Port: base, Protocol: corev1.ProtocolTCP, TargetPort: intstr.FromInt32(base)},
		{Name: "mysql-ro", Port: base + 1, Protocol: corev1.ProtocolTCP, TargetPort: intstr.FromInt32(base + 1)},
	}
	if router.Spec.Routing.ExposeXProtocol {
		ports = append(ports,
			corev1.ServicePort{Name: "mysqlx-rw", Port: base + 2, Protocol: corev1.ProtocolTCP, TargetPort: intstr.FromInt32(base + 2)},
			corev1.ServicePort{Name: "mysqlx-ro", Port: base + 3, Protocol: corev1.ProtocolTCP, TargetPort: intstr.FromInt32(base + 3)},
		)
	}
	return ports
}

func routerContainerPorts(router *routerv1alpha1.MySQLRouter) []corev1.ContainerPort {
	base := router.Spec.Routing.BasePort
	return []corev1.ContainerPort{
		{Name: "mysql-rw", ContainerPort: base, Protocol: corev1.ProtocolTCP},
		{Name: "mysql-ro", ContainerPort: base + 1, Protocol: corev1.ProtocolTCP},
		{Name: "mysqlx-rw", ContainerPort: base + 2, Protocol: corev1.ProtocolTCP},
		{Name: "mysqlx-ro", ContainerPort: base + 3, Protocol: corev1.ProtocolTCP},
	}
}

func bootstrapScript(router *routerv1alpha1.MySQLRouter) string {
	baseArgs := []string{
		"mysqlrouter",
		`--bootstrap \"${MYSQL_BOOTSTRAP_USER}@${MYSQL_BOOTSTRAP_HOST}:${MYSQL_BOOTSTRAP_PORT}\"`,
		"--directory /router",
		`--conf-base-port \"${MYSQL_ROUTER_BASE_PORT}\"`,
		`--conf-bind-address \"${MYSQL_ROUTER_BIND_ADDRESS}\"`,
	}
	for _, arg := range router.Spec.Bootstrap.ExtraArgs {
		baseArgs = append(baseArgs, shellQuote(arg))
	}

	return strings.Join([]string{
		"set -eu",
		"mkdir -p /router",
		"if [ ! -f /router/mysqlrouter.conf ]; then",
		fmt.Sprintf("  printf '%%s\\n' \"${MYSQL_BOOTSTRAP_PASSWORD}\" | %s", strings.Join(baseArgs, " ")),
		"fi",
		"exec mysqlrouter -c /router/mysqlrouter.conf",
	}, "\n")
}

func storageVolume(router *routerv1alpha1.MySQLRouter) (corev1.Volume, corev1.VolumeMount) {
	volume := corev1.Volume{Name: "router-data"}
	switch router.Spec.Storage.Type {
	case "PersistentVolumeClaim":
		volume.VolumeSource = corev1.VolumeSource{
			PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
				ClaimName: router.Spec.Storage.ClaimName,
			},
		}
	default:
		volume.VolumeSource = corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}
	}

	return volume, corev1.VolumeMount{Name: volume.Name, MountPath: "/router"}
}

func secretEnvVar(name string, ref routerv1alpha1.SecretKeyRef) corev1.EnvVar {
	return corev1.EnvVar{
		Name: name,
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: ref.Name},
				Key:                  ref.Key,
			},
		},
	}
}

func derivePhase(deployment *appsv1.Deployment) string {
	desired := valueOrDefault(deployment.Spec.Replicas, 1)
	switch {
	case deployment.Status.ReadyReplicas >= desired && desired > 0:
		return "Ready"
	case deployment.Status.UpdatedReplicas > 0 || deployment.Status.AvailableReplicas > 0:
		return "Progressing"
	default:
		return "Pending"
	}
}

func valueOrDefault(v *int32, d int32) int32 {
	if v == nil {
		return d
	}
	return *v
}

func ptrTo[T any](v T) *T {
	return &v
}

func shellQuote(v string) string {
	return "'" + strings.ReplaceAll(v, "'", `'"'"'`) + "'"
}

func sortedCopy(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}
