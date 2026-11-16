# High availability

RadFetch can run with redundant services, for example on Docker Swarm, so that
losing a container or a node does not stop it. This page says what is redundant,
what is not, and what a cluster needs.

## What makes it possible

- **All state is in Postgres.** No service keeps anything that matters in memory.
- **Work is claimed with leases.** If a service dies mid-task, the lease expires and another instance takes the task. Nothing is lost and nothing needs cleaning up.
- **Single-instance roles elect a leader.** The gateway must be the only thing talking to the PACS. Several gateway replicas can run: one holds a database lock and works, the others stand by. The lock is released by the database the moment the holder's connection ends, so a standby takes over within seconds.

## Per service

| Service | Replicas | If one dies |
|---|---|---|
| `api` | Any number, behind the cluster's load balancer | The others keep serving. A user signed in stays signed in (sessions are in the database) |
| `gateway` | 2 or more; one active, the rest standby | A standby becomes active within seconds. A series that was being fetched is fetched again |
| `scp` (push ingress) | Any number receive; one turns studies into jobs | Another takes over. A sender whose connection was cut must resend that study |
| `sender` | Any number | The others continue; an interrupted delivery is retried |
| `janitor` | 1 | Cleanup pauses until it is restarted. Nothing breaks |
| `executor-gpu` | One per GPU, on the node that has the GPU | See below |
| `executor-cpu` | Any number, on any node | Its step is run again by another CPU executor after the lease expires |
| `postgres` | **Not redundant in the example stack** | Everything pauses until it is back, then continues |

## Limits to be aware of

**Postgres is the single point of failure.** The example stack runs one Postgres
on one node. For real high availability, use a replicated Postgres (Patroni,
CloudNativePG, or a managed service) and point `RADFETCH_DATABASE_URL` at it.
All services reconnect by themselves when the database comes back or fails over.

**A running step does not move.** A flow container runs on the node whose
executor started it. If that node is lost, the container is lost; after the
executor's lease expires (two minutes) another executor of the same kind runs
that step again. Steps of the job that had already succeeded are kept. If only the executor service restarts and the node stays up, the
container keeps running and is picked up again.

**The data folder must be shared.** The cache, run files and incoming studies
have to be on one filesystem that every node mounts at the same path (NFS,
CephFS or similar), because:

- any gateway replica writes the cache and any executor reads it;
- run inputs are hardlinks into the cache, which only work within one filesystem;
- executors start flow containers through their node's Docker daemon with bind mounts, so the path must exist on that node.

That shared filesystem then has to be highly available itself.

**GPU capacity is per node.** Executors are pinned to GPU nodes. Losing a GPU
node loses its capacity until it returns.

**In C-MOVE mode the PACS connects back** to the storage SCP on port 11113. With
several gateway replicas the cluster's load balancer may hand that connection to
a standby, which refuses data it did not ask for; the retrieval then fails and
is retried. Prefer `get` or `dicomweb` mode in a cluster, or publish port 11113
in host mode and run the gateway on one fixed node.

## How flow containers run in a cluster

Flow containers are **not** Swarm services. The executor is a Swarm service (one
per GPU node), and it starts each flow container as an ordinary container on its
own node, through that node's Docker socket. This is the same "sibling container"
arrangement as on a single host.

What follows from that:

- **Swarm does not see or reschedule flow containers.** They do not appear in `docker service ls`. RadFetch itself tracks them (each carries a `radfetch.run_id` label) and recovers through its leases.
- **A step runs on the node of the executor that took it.** The steps of one job can run on different nodes: a GPU step on a GPU node, its CPU steps wherever a CPU executor is free. GPU nodes are chosen by labelling them, not per job.
- **GPUs work as on a single host**, through the NVIDIA Container Toolkit on the node. This is the main reason for not using Swarm services: Swarm has no equivalent of handing a service task a GPU on request.
- **The data folder must be at the same path on every node**, because the bind mounts are resolved by the node's own Docker daemon.
- **The Docker socket is root-equivalent** on each GPU node, as on a single host.

Running flow containers as scheduled cluster jobs instead (Swarm jobs or
Kubernetes Jobs) would need another implementation of the `Runtime` interface;
see [Development](development.md).

## Docker Swarm

`deploy/swarm-stack.yml` is an example stack. Before deploying:

1. Mount the shared filesystem at the same path on every node (default `/srv/radfetch`), with `data/` and `flows/` in it.
2. Push the image to a registry all nodes can pull from and set `RADFETCH_IMAGE`.
3. Label the nodes:

    ```bash
    docker node update --label-add radfetch.gpu=true <gpu-node>
    ```

    ```bash
    docker node update --label-add radfetch.db=true <database-node>
    ```

4. Set the PACS settings as environment variables in the shell you deploy from (the stack file reads them), then:

    ```bash
    docker stack deploy -c deploy/swarm-stack.yml radfetch
    ```

5. Create the first administrator (on a manager node):

    ```bash
    docker exec -it $(docker ps -q -f name=radfetch_api | head -1) radfetch user create anna --role admin
    ```

Notes on the stack:

- Services start in any order. Until the `init-db` service has applied the schema, the others log database errors and retry; this settles within seconds.
- `init-db` runs once and exits; `docker service ls` shows it as `0/1`, which is expected.
- Secrets (database password, DICOMweb token) are plain environment variables in the example. Use Docker secrets or your own mechanism in production.
- For a multi-GPU node, run one GPU executor service per GPU with `RADFETCH_GPU_DEVICE_IDS`, as described in [Administration](administration.md).
- `executor-cpu` runs on any node. To keep CPU steps off certain nodes, add a placement constraint to it.

## What has been tested

- Leader election and standby takeover for the gateway and the ingress processor, in the automated tests.
- The stack file on a **single-node** swarm: all services started, jobs ran, and after killing the active gateway container the standby took over within about a second and the next job completed.

Not tested: a swarm with several nodes, a shared network filesystem, node loss,
Swarm's routing mesh for the published ports, and a replicated Postgres. Treat
the stack file as a starting point and rehearse the failures you care about
before relying on it.

## Kubernetes

Nothing in the design is specific to Swarm: the same replica counts apply. The
executor, however, starts flow containers through a Docker socket, which most
Kubernetes clusters do not offer. Running flows as Kubernetes Jobs needs a
second implementation of the `Runtime` interface (see [Development](development.md));
that has not been written.
