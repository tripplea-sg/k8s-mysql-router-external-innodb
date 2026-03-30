In bash:
```
pip install -r requirements-operator.txt
kopf run mysqlrouter_controller_kopf.py --verbose
```
For in-cluster use, the controller container command should be:
```
pip install -r requirements-operator.txt
kopf run mysqlrouter_controller_kopf.py --verbose
```
Deploy the Router image <br>
-----------------------
Use this Dockerfile in the same directory as mysqlrouter_controller_kopf.py and requirements-operator.txt:
```
FROM python:3.12-slim

WORKDIR /app

COPY requirements-operator.txt .
RUN pip install --no-cache-dir -r requirements-operator.txt

COPY mysqlrouter_controller_kopf.py /app/mysqlrouter_controller_kopf.py

CMD ["kopf", "run", "/app/mysqlrouter_controller_kopf.py", "--verbose", "--standalone", "-A"]
```
Why these flags: <br>
kopf run <file> is the normal way to run a handler file. <br>
-A means all namespaces. <br>
--standalone disables peering, which keeps this starter operator simpler. <br>
Build and push:
```
docker build -t ghcr.io/YOUR_GITHUB_USERNAME/mysql-router-operator:0.1.0 .
docker push ghcr.io/YOUR_GITHUB_USERNAME/mysql-router-operator:0.1.0
```
Kopf’s deployment docs explicitly show the pattern “build image, push image, run the operator from that image,” and recommend Python 3.10+ in the image. <br>
Apply CRD:
```
kubectl apply -f config/crd/bases/router.example.com_mysqlrouters.yaml
```
Verify:
```
kubectl get crd mysqlrouters.router.example.com
```
Use the following manifest:
```
apiVersion: v1
kind: Namespace
metadata:
  name: mysql-router-operator-system
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mysql-router-operator
  namespace: mysql-router-operator-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: mysql-router-operator
rules:
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["router.example.com"]
    resources: ["mysqlrouters"]
    verbs: ["get", "list", "watch", "patch", "update"]
  - apiGroups: ["router.example.com"]
    resources: ["mysqlrouters/status"]
    verbs: ["get", "patch", "update"]
  - apiGroups: ["router.example.com"]
    resources: ["mysqlrouters/finalizers"]
    verbs: ["update"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
  - apiGroups: ["events.k8s.io"]
    resources: ["events"]
    verbs: ["create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: mysql-router-operator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: mysql-router-operator
subjects:
  - kind: ServiceAccount
    name: mysql-router-operator
    namespace: mysql-router-operator-system
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mysql-router-operator
  namespace: mysql-router-operator-system
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: mysql-router-operator
  template:
    metadata:
      labels:
        app.kubernetes.io/name: mysql-router-operator
    spec:
      serviceAccountName: mysql-router-operator
      containers:
        - name: operator
          image: ghcr.io/YOUR_GITHUB_USERNAME/mysql-router-operator:0.1.0
          imagePullPolicy: IfNotPresent
          command:
            - kopf
          args:
            - run
            - /app/mysqlrouter_controller_kopf.py
            - --verbose
            - --standalone
            - -A

```
Apply it:
```
kubectl apply -f operator.yaml
kubectl get pods -n mysql-router-operator-system
kubectl logs -n mysql-router-operator-system deploy/mysql-router-operator
```
Create the application namespace
```
kubectl create namespace mysql-router
```
Create the bootstrap Secret: For production, the recommended MySQL approach is to pre-create a dedicated Router account with setupRouterAccount() instead of repeatedly bootstrapping with a high-privilege account.
```
apiVersion: v1
kind: Secret
metadata:
  name: mysql-router-bootstrap
  namespace: mysql-router
type: Opaque
stringData:
  username: mysqlrouter
  password: CHANGE_ME
```
Apply it:
```
kubectl apply -f mysql-router-bootstrap-secret.yaml
```
Create PVC for Router state:
```
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-router-pvc
  namespace: mysql-router
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```
Create MySQL Router Resource:
```
apiVersion: router.example.com/v1alpha1
kind: MySQLRouter
metadata:
  name: mysql-router
  namespace: mysql-router
spec:
  image: container-registry.oracle.com/mysql/community-router:8.4
  imagePullPolicy: IfNotPresent
  replicas: 1
  serviceType: ClusterIP
  serviceAnnotations: {}

  bootstrap:
    nodeName: node-0
    usernameSecretRef:
      name: mysql-router-bootstrap
      key: username
    passwordSecretRef:
      name: mysql-router-bootstrap
      key: password
    extraArgs: []

  routing:
    bindAddress: 0.0.0.0
    basePort: 6446
    exposeXProtocol: true

  storage:
    type: PersistentVolumeClaim
    claimName: mysql-router-pvc

  externalNodes:
    - name: node-0
      address: 10.10.1.11
      port: 3306
    - name: node-1
      address: 10.10.1.12
      port: 3306
    - name: node-2
      address: 10.10.1.13
      port: 3306
```
Check status:
```
kubectl get mysqlrouters -n mysql-router
kubectl describe mysqlrouter mysql-router -n mysql-router
kubectl get deploy,svc,endpointslice -n mysql-router
kubectl get pods -n mysql-router
kubectl logs -n mysql-router deploy/mysql-router
```
Upgrade:
```
kubectl patch mysqlrouter mysql-router \
  -n mysql-router \
  --type merge \
  -p '{"spec":{"image":"container-registry.oracle.com/mysql/community-router:9.7.0"}}'
```
Quick Command reference:
```
# 1) install CRD
kubectl apply -f config/crd/bases/router.example.com_mysqlrouters.yaml

# 2) deploy operator
kubectl apply -f operator.yaml

# 3) create app namespace + secret + pvc
kubectl create namespace mysql-router
kubectl apply -f mysql-router-bootstrap-secret.yaml
kubectl apply -f mysql-router-pvc.yaml

# 4) create mysqlrouter CR
kubectl apply -f mysqlrouter.yaml

# 5) verify
kubectl get mysqlrouters -n mysql-router
kubectl get deploy,svc,endpointslice,pods -n mysql-router
kubectl logs -n mysql-router deploy/mysql-router

# 6) later, upgrade to 9.7 when tag exists
kubectl patch mysqlrouter mysql-router \
  -n mysql-router \
  --type merge \
  -p '{"spec":{"image":"container-registry.oracle.com/mysql/community-router:9.7.0"}}'

kubectl rollout status deployment/mysql-router -n mysql-router
```
