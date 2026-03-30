Script involved:
```
scripts/generate_mysql_router_manifest.py
scripts/list_mysql_router_status.py
config/router-config.yaml
generated/mysql-router.yaml
```
Prepare working directory:
```
mkdir -p mysql-router-k8s-manifest-generator/{scripts,config,generated}
cd mysql-router-k8s-manifest-generator
```
Put this file in place:
```
scripts/generate_mysql_router_manifest.py
scripts/list_mysql_router_status.py
requirements.txt
```
Use this requirements.txt
```
PyYAML>=6.0
kubernetes
```
Create a virtual environment and install:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
Kubernetes documents that the Python client can use the same kubeconfig as kubectl when run locally, and in-cluster credentials when run from a Pod. <br>
Verify kubernetes access list:
```
kubectl get nodes
kubectl cluster-info
```
Choose the bootstrap approach that matches your current generator <br>

There are two possible patterns: <bd>
1. Recommended by MySQL long term: pre-create a Router account with setupRouterAccount(). <br>
2. Simplest with your current generator as written: use an InnoDB Cluster admin/bootstrap account in router-config.yaml.<br>

Why I recommend the second option for now: <br>
MySQL documents that setupRouterAccount() is the recommended way to create the Router user, but bootstrap flows that use --account can prompt 
for both the bootstrap account password and the Router account password. Your current generator only feeds one password into mysqlrouter, 
so the cleanest path with the current code is to use a bootstrap/admin account and persist /router so Router does not keep re-bootstrapping <br>
Create namespace:
```
kubectl create namespace mysql-router
```
Create PVC for route state: <br>
This is the safer choice with your current generator, because Router writes generated state under /router, and you do not want to lose that on Pod recreation.
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
Apply it:
```
kubectl apply -f mysql-router-pvc.yaml
```
Create router-config.yaml something like:
```
namespace: mysql-router
create_namespace: false

router:
  name: mysql-router
  service_name: mysql-router
  service_type: ClusterIP
  image: container-registry.oracle.com/mysql/community-router:8.4
  replicas: 1
  secret_name: mysql-router-bootstrap
  bootstrap_config_dir: /router

  bind_address: 0.0.0.0
  http_bind_address: 0.0.0.0

  expose_x_protocol: false
  expose_http_api: false

  ports:
    rw: 6446
    ro: 6447
    x_rw: 6448
    x_ro: 6449
    http: 8443

  bootstrap:
    user: icadmin
    password: CHANGE_ME
    host: mysql-node-0
    port: 3306
    extra_args: []

  storage:
    type: pvc
    pvc_name: mysql-router-pvc

  probes:
    port: 6446
    readiness_initial_delay_seconds: 10
    liveness_initial_delay_seconds: 30

nodes:
  - name: mysql-node-0
    ip: 10.10.1.11
    port: 3306
  - name: mysql-node-1
    ip: 10.10.1.12
    port: 3306
  - name: mysql-node-2
    ip: 10.10.1.13
    port: 3306
```
Notes: <br>
bootstrap.host: mysql-node-0 should match one of the generated per-node Kubernetes Service names. <br>
nodes[].ip and nodes[].port are the VM IPs and MySQL ports. <br>
Pinning the Router image tag is better than using a floating image. MySQL documents the Oracle Container Registry image path for MySQL Community Router and supports version tags <br>
Make sure Pods can reach InnoDB Cluster network:
```
kubectl -n mysql-router run netcheck \
  --rm -it \
  --restart=Never \
  --image=busybox:1.36 \
  -- sh
```
Inside the shell:
```
nc -zvw3 10.10.1.11 3306
nc -zvw3 10.10.1.12 3306
nc -zvw3 10.10.1.13 3306
```
Generate the Kubernetes manifest:
```
python scripts/generate_mysql_router_manifest.py \
  -c config/router-config.yaml \
  -o generated/mysql-router.yaml
```
This should generate: <br>
one Secret for bootstrap <br>
one headless Service per VM node <br>
one Endpoints object per VM node <br>
one MySQL Router Deployment <br>
one MySQL Router Service <br>
Your approach relies on a Service-without-selector pattern so that Kubernetes service names can point at manually defined external backends. <br>
Kubernetes documents that this pattern works, though it now recommends EndpointSlice as the modern API instead of the legacy Endpoints API. <br>
Review the generated manifest:
```
less generated/mysql-router.yaml
```
Check these things: <br>
namespace is mysql-router <br>
the Secret contains the right bootstrap user/password <br>
mysql-node-0, mysql-node-1, mysql-node-2 map to the correct VM IPs and ports <br>
Deployment image and ports are correct <br>
PVC name matches mysql-router-pvc <br>
Apply manifest:
```
kubectl apply -f generated/mysql-router.yaml
```
Then verify:
```
kubectl -n mysql-router get deploy,pod,svc,endpoints
kubectl -n mysql-router describe deployment mysql-router
kubectl -n mysql-router logs deploy/mysql-router
```
Check status with output in table:
```
python scripts/list_mysql_router_status.py -n mysql-router --output table
```
Check status with output in JSON:
```
python scripts/list_mysql_router_status.py -n mysql-router --output json
```
Because the script uses the Kubernetes Python client, it should work with the same kubeconfig you already use for kubectl. <br>
Test connection with MySQL Router <br>
1. Option A
  ```
kubectl -n mysql-router port-forward svc/mysql-router 6446:6446
```
3. Option B
```
kubectl -n mysql-router run mysql-client \
  --rm -it \
  --restart=Never \
  --image=mysql:8.4 \
  -- bash
```
Inside the pod:
```
mysql -h mysql-router -P 6446 -u your_app_user -p -e "select @@hostname, @@port"
```
