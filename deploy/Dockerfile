ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.9.14@sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294
FROM ${HERMES_BASE_IMAGE}

ARG HERMES_BASE_IMAGE
ARG HERMES_HELMET_SOURCE_COMMIT=unknown
ARG HERMES_HELMET_VERSION=0.0.0-dev
ARG GH_CLI_VERSION=2.101.0
ARG GH_CLI_SHA256_AMD64=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
ARG GH_CLI_SHA256_ARM64=b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f
ARG TARGETARCH
ARG HERMES_UID=10000
ARG HERMES_GID=10000

LABEL org.opencontainers.image.title="Hermes Helmet"
LABEL org.opencontainers.image.description="Portable GitHub-to-Hermes control loop"
LABEL org.opencontainers.image.source="https://github.com/MachineWisdomAI/hermes-helmet-oss"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.revision="${HERMES_HELMET_SOURCE_COMMIT}"
LABEL org.opencontainers.image.version="${HERMES_HELMET_VERSION}"
LABEL org.opencontainers.image.base.name="${HERMES_BASE_IMAGE}"
LABEL com.machinewisdom.hermes-helmet.source-commit="${HERMES_HELMET_SOURCE_COMMIT}"

ENV PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:${PATH}"
ENV PYTHONPATH="/opt/hermes-helmet/src"
ENV HERMES_UID=${HERMES_UID}
ENV HERMES_GID=${HERMES_GID}

USER root

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) gh_sha256="${GH_CLI_SHA256_AMD64}" ;; \
      arm64) gh_sha256="${GH_CLI_SHA256_ARM64}" ;; \
      *) echo "unsupported GitHub CLI architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    gh_archive="gh_${GH_CLI_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSLo "/tmp/${gh_archive}" \
      "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/${gh_archive}"; \
    printf '%s  %s\n' "${gh_sha256}" "/tmp/${gh_archive}" | sha256sum -c -; \
    mkdir -p /usr/local/libexec/hermes-helmet; \
    tar -xzf "/tmp/${gh_archive}" -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_CLI_VERSION}_linux_${TARGETARCH}/bin/gh" \
      /usr/local/libexec/hermes-helmet/gh; \
    /usr/local/libexec/hermes-helmet/gh --version | head -n 1 \
      | grep -F "gh version ${GH_CLI_VERSION} "; \
    rm -rf "/tmp/${gh_archive}" "/tmp/gh_${GH_CLI_VERSION}_linux_${TARGETARCH}"; \
    ln -sf /opt/hermes/.venv/bin/hermes /usr/local/bin/hermes; \
    if ! getent group "${HERMES_GID}" >/dev/null; then groupadd -g "${HERMES_GID}" hermes; fi; \
    if ! getent passwd "${HERMES_UID}" >/dev/null; then \
         useradd --uid "${HERMES_UID}" --gid "${HERMES_GID}" --home-dir /opt/data --no-create-home \
           --shell /usr/sbin/nologin hermes; \
       fi; \
    mkdir -p /opt/data /opt/hermes-helmet; \
    chown -R "${HERMES_UID}:${HERMES_GID}" /opt/data

# Control-plane tree stays root-owned and non-writable by the worker. Only
# mutable runtime paths (/opt/data, /run/hermes-helmet) are worker-owned.
COPY src /opt/hermes-helmet/src
COPY skills /opt/hermes-helmet/src/hermes_helmet/bundled_skills
COPY config /opt/hermes-helmet/config
COPY deploy/hermes/runtime-entrypoint.sh /opt/hermes-helmet/runtime-entrypoint.sh
COPY deploy/hermes/gh-runtime-wrapper.sh /usr/local/bin/gh
RUN printf '%s\n' "${HERMES_HELMET_SOURCE_COMMIT}" > /opt/hermes-helmet/SOURCE_COMMIT \
    && chown -R root:root /opt/hermes-helmet /usr/local/bin/gh \
    && chmod -R a+rX /opt/hermes-helmet \
    && chmod -R go-w /opt/hermes-helmet \
    && chmod 755 /opt/hermes-helmet/runtime-entrypoint.sh /usr/local/bin/gh \
    && chmod 644 /opt/hermes-helmet/SOURCE_COMMIT \
    && chmod go-w /usr/local/bin/gh

# Named volumes inherit ownership from this image path on first creation.
WORKDIR /opt/data
USER ${HERMES_UID}:${HERMES_GID}
ENTRYPOINT ["/opt/hermes-helmet/runtime-entrypoint.sh"]
CMD ["gateway", "run"]
