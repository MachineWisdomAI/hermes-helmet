ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v0.21.6@sha256:55e192fba0cd4fde61142abbff5adeacff40efdb482ccd0ff877924bd274f909
FROM ${HERMES_BASE_IMAGE}

ARG HERMES_BASE_IMAGE
ARG HERMES_HELMET_SOURCE_COMMIT=unknown
ARG HERMES_HELMET_VERSION=0.0.0-dev
ARG GH_CLI_VERSION=2.101.0
ARG GH_CLI_SHA256_AMD64=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
ARG GH_CLI_SHA256_ARM64=b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f
ARG CLAUDE_CODE_VERSION=2.1.286
ARG CLAUDE_CODE_SHA256_AMD64=fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f
ARG CLAUDE_CODE_SHA256_ARM64=0292fa22ac2fd43e16be9d0e511ddd8347280d6e0ebaca744ef5b27e05d8d0f8
ARG CLAUDE_SUBSCRIPTION_PLUGIN_COMMIT=4bc79c78031d1a042b5d8a7314ceea283db5c5e2
ARG CLAUDE_SUBSCRIPTION_PLUGIN_VERSION=0.3.3
ARG TARGETARCH
ARG HERMES_UID=10000
ARG HERMES_GID=10000

LABEL org.opencontainers.image.title="Hermes Helmet"
LABEL org.opencontainers.image.description="Portable GitHub-to-Hermes control loop"
LABEL org.opencontainers.image.source="https://github.com/MachineWisdomAI/hermes-helmet"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.revision="${HERMES_HELMET_SOURCE_COMMIT}"
LABEL org.opencontainers.image.version="${HERMES_HELMET_VERSION}"
LABEL org.opencontainers.image.base.name="${HERMES_BASE_IMAGE}"
LABEL com.machinewisdom.hermes-helmet.source-commit="${HERMES_HELMET_SOURCE_COMMIT}"

ENV PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:${PATH}"
ENV PYTHONPATH="/opt/hermes-helmet/src"
ENV HERMES_UID=${HERMES_UID}
ENV HERMES_GID=${HERMES_GID}
# The Claude Code binary is root-owned; never let it try to replace itself.
ENV DISABLE_AUTOUPDATER=1

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

# Claude Code native binary for the Claude subscription model provider.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) claude_platform=linux-x64; claude_sha256="${CLAUDE_CODE_SHA256_AMD64}" ;; \
      arm64) claude_platform=linux-arm64; claude_sha256="${CLAUDE_CODE_SHA256_ARM64}" ;; \
      *) echo "unsupported Claude Code architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/claude \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CODE_VERSION}/${claude_platform}/claude"; \
    printf '%s  %s\n' "${claude_sha256}" /tmp/claude | sha256sum -c -; \
    install -m 0755 /tmp/claude /usr/local/libexec/hermes-helmet/claude; \
    rm -f /tmp/claude; \
    HOME=/tmp/claude-version-home /usr/local/libexec/hermes-helmet/claude --version \
      | grep -F "${CLAUDE_CODE_VERSION}"; \
    rm -rf /tmp/claude-version-home

# Claude subscription model provider, fetched by full commit and bundled
# root-owned beside the upstream providers so every profile can select it.
RUN set -eux; \
    plugin_src=/tmp/claude-subscription-plugin; \
    plugin_dir=/opt/hermes/plugins/model-providers/claude-subscription-directsdk-experimental; \
    git init -q "$plugin_src"; \
    git -C "$plugin_src" fetch -q --depth 1 \
      https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk.git \
      "${CLAUDE_SUBSCRIPTION_PLUGIN_COMMIT}"; \
    git -C "$plugin_src" checkout -q FETCH_HEAD; \
    test "$(git -C "$plugin_src" rev-parse HEAD)" = "${CLAUDE_SUBSCRIPTION_PLUGIN_COMMIT}"; \
    grep -qx "version: ${CLAUDE_SUBSCRIPTION_PLUGIN_VERSION}" "$plugin_src/plugin.yaml"; \
    install -d -m 0755 "$plugin_dir"; \
    for file in __init__.py admission.py directsdk.py directsdk_setup.py inert_mcp.py \
        model_catalog.py plugin.yaml LICENSE; do \
      install -m 0644 "$plugin_src/$file" "$plugin_dir/$file"; \
    done; \
    rm -rf "$plugin_src"; \
    chown -R root:root "$plugin_dir"

# Control-plane tree stays root-owned and non-writable by the worker. Only
# mutable runtime paths (/opt/data, /run/hermes-helmet) are worker-owned.
COPY src /opt/hermes-helmet/src
COPY skills /opt/hermes-helmet/src/hermes_helmet/bundled_skills
COPY config /opt/hermes-helmet/config
COPY deploy/hermes/runtime-entrypoint.sh /opt/hermes-helmet/runtime-entrypoint.sh
COPY deploy/hermes/gh-runtime-wrapper.sh /usr/local/bin/gh
COPY deploy/hermes/claude-runtime-wrapper.sh /usr/local/bin/claude
RUN printf '%s\n' "${HERMES_HELMET_SOURCE_COMMIT}" > /opt/hermes-helmet/SOURCE_COMMIT \
    && chown -R root:root /opt/hermes-helmet /usr/local/bin/gh /usr/local/bin/claude \
    && chmod -R a+rX /opt/hermes-helmet \
    && chmod -R go-w /opt/hermes-helmet \
    && chmod 755 /opt/hermes-helmet/runtime-entrypoint.sh /usr/local/bin/gh /usr/local/bin/claude \
    && chmod 644 /opt/hermes-helmet/SOURCE_COMMIT \
    && chmod go-w /usr/local/bin/gh /usr/local/bin/claude

# Named volumes inherit ownership from this image path on first creation.
WORKDIR /opt/data
USER ${HERMES_UID}:${HERMES_GID}
ENTRYPOINT ["/opt/hermes-helmet/runtime-entrypoint.sh"]
CMD ["gateway", "run"]
