# Flink 1.20 with Python support for PyFlink jobs (DESIGN.md Deployment).
#
# The official flink image has no Python — but PyFlink jobs execute their
# Python functions in worker processes on every TaskManager, so the runtime
# (python + apache-flink + our deps) must exist inside the image. The pm
# package itself is NOT baked in: the repo is volume-mounted at /opt/pm and
# exposed via PYTHONPATH, so code changes don't require an image rebuild.

FROM flink:1.20.2-scala_2.12-java11

# Base is Ubuntu jammy -> Python 3.10 (supported by PyFlink 1.20; pm's code
# floor is 3.10+ syntax). Jobs are submitted from INSIDE the cluster
# (make submit-enrich), so client and workers always share this interpreter —
# cloudpickle is never asked to cross Python minor versions.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# pyflink version must match the flink runtime version
RUN pip3 install --no-cache-dir \
    "apache-flink==1.20.5" \
    "pydantic>=2.7" \
    "confluent-kafka>=2.4" \
    "clickhouse-connect>=0.7" \
    "boto3>=1.34"

# Pre-create the checkpoint dir owned by the flink user: Docker initializes
# a NEW named volume with the ownership of the image path it mounts over.
# Without this the volume is root-owned and the JobManager's checkpoint
# coordinator fails with "Failed to create directory for shared state".
RUN mkdir -p /flink-checkpoints && chown flink:flink /flink-checkpoints

ENV PYTHONPATH=/opt/pm/src
