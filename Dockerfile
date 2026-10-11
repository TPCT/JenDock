FROM node:22-alpine AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a AS receiver
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir cryptography==48.0.1
COPY workload_environment.py managed_lifecycle.py application_credentials.py application_database_bindings.py service_runtime.py service_runtime_operations.py adopted_workloads.py migration_status.py migration_jobs.py migration_bindings.py migration_credentials.py database_bindings.py database_rpc.py domain_authority.py domain_external_flow.py domain_execution.py ingress_route_generations.py cloudflare_dns.py ingress.py ingress_bindings.py ingress_capacity.py mcp_credentials.py application_operations.py server.py store.py telemetry.py resource_policy.py registry.py job_target.py delivery_target.py integrations.py workspace.py direct_limits.py adoption.py compose_identity.py provenance.py provenance_store.py compose_reconciliation.py compose_review.py container_logs.py ownership.py build_profiles.py managed_delivery.py managed_services.py ./
COPY --from=frontend /ui/dist ./web
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)"
CMD ["python", "server.py"]

FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a AS worker
RUN apk add --no-cache git openssh-client ca-certificates && pip install --no-cache-dir PyYAML==6.0.3 cryptography==48.0.1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY workload_environment.py managed_lifecycle.py application_credentials.py application_database_bindings.py application_database_networks.py service_runtime.py service_runtime_operations.py adopted_workloads.py migration_status.py migration_jobs.py migration_bindings.py migration_credentials.py migration_coordinator.py migration_runner.py runtime_proof_refresh.py image_cleanup.py database_target.py database_candidates.py database_inspection.py database_inventory.py database_helper.py database_auth.py database_bindings.py database_control.py database_rpc.py domain_renewal.py ingress_proof_refresh.py domain_authority.py ingress_external.py domain_external_flow.py domain_execution.py domain_worker.py ingress_route_generations.py ingress_host_transport.py ingress_host_service.py cloudflare_dns.py ingress.py ingress_bindings.py ingress_runtime.py ingress_ports.py ingress_capacity.py container_file_changes.py container_files.py mcp_credentials.py worker.py store.py control.py terminal_control.py telemetry.py resource_policy.py registry.py job_target.py delivery_target.py integrations.py workspace.py direct_limits.py adoption.py compose_identity.py provenance.py provenance_store.py compose_reconciliation.py compose_review.py container_logs.py ownership.py build_profiles.py managed_delivery.py managed_rollout.py application_operations.py managed_services.py service_provisioning.py artifact_publication.py ./
CMD ["python", "worker.py"]

FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a AS mcp
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements-mcp.txt cryptography==48.0.1
COPY --from=receiver /app/*.py ./
COPY mcp_server.py ./
USER 65534:65534
EXPOSE 8081
CMD ["uvicorn", "mcp_server:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8081", "--no-proxy-headers", "--no-access-log"]
