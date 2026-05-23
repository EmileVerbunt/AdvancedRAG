"""Microsoft GraphRAG runner: scaffolds Azure-aware config and runs the indexer.

graphrag (>=2.x) consumes raw `input/*.txt` files, does its own chunking,
extraction, and community detection — so we feed it the chunk text we already
have and let it build community summaries. The pre-extracted ontology graph we
produce ourselves is a *separate* artifact.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import textwrap
from collections.abc import Iterable
from pathlib import Path

from knowledge_extraction.config.settings import AzureAuthMode, Settings
from knowledge_extraction.domain import Chunk, OntologyVersion

log = logging.getLogger(__name__)


def resolve_graphrag_executable(settings: Settings) -> str:
    """Return a runnable graphrag entry point.

    Resolution order:
      1. ``settings.graphrag_executable`` (env var ``GRAPHRAG_EXECUTABLE``)
      2. ``graphrag`` on PATH
      3. ``<repo>/.graphrag-venv/Scripts/graphrag.exe`` (Windows venv created
         by setup scripts)
      4. Common short-path Windows fallback ``C:\\g\\Scripts\\graphrag.exe``

    Raises RuntimeError with a clear remediation message if none work.
    """
    if settings.graphrag_executable:
        if Path(settings.graphrag_executable).exists():
            return settings.graphrag_executable
        log.warning("settings.graphrag_executable=%s does not exist", settings.graphrag_executable)

    on_path = shutil.which("graphrag")
    if on_path:
        return on_path

    candidates = [
        settings.project_root / ".graphrag-venv" / "Scripts" / "graphrag.exe",
        Path("C:/g/Scripts/graphrag.exe"),
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)

    raise RuntimeError(
        "Could not find the `graphrag` executable. Install it with "
        "`pip install graphrag>=2.0.0` (use a short-path venv on Windows to "
        "avoid the litellm long-path bug), or set GRAPHRAG_EXECUTABLE."
    )


class GraphRagRunner:
    """Wraps the official `graphrag` CLI with Azure OpenAI settings derived from our config."""

    def __init__(self, root: Path, settings: Settings) -> None:
        self._root = root
        self._settings = settings
        self._exe = resolve_graphrag_executable(settings)

    @property
    def executable(self) -> str:
        return self._exe

    def workdir(self, version: OntologyVersion) -> Path:
        d = self._root / version.version
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_inputs(self, version: OntologyVersion, chunks: Iterable[Chunk]) -> Path:
        wd = self.workdir(version)

        # 1) Scaffold prompts / .env / settings.yaml via the official `graphrag init`.
        proc = asyncio.run(self._init(wd))
        if proc != 0:
            raise RuntimeError(f"graphrag init failed with exit code {proc}")

        # 2) Overwrite settings.yaml with our Azure-aware config.
        (wd / "settings.yaml").write_text(self._azure_settings_yaml(), encoding="utf-8")

        # 3) Overlay any custom prompt templates we ship in config/graphrag_prompts/.
        #    `graphrag init --force` regenerates the default prompts every time, so
        #    customizations have to be re-applied here. The local_search prompt in
        #    particular trades the default's strict refusal stance for a graceful
        #    fallback (see config/graphrag_prompts/local_search_system_prompt.txt).
        self._overlay_custom_prompts(wd)

        # 4) Write .env. In KEY mode we provide GRAPHRAG_API_KEY for the templated
        #    settings.yaml. In CREDENTIAL mode graphrag uses DefaultAzureCredential
        #    via `auth_method: azure_managed_identity`, so we just leave a placeholder
        #    so dotenv loading doesn't choke.
        if self._settings.azure_auth_mode == AzureAuthMode.KEY:
            if not self._settings.azure_openai_api_key:
                raise RuntimeError(
                    "AZURE_AUTH_MODE=key but AZURE_OPENAI_API_KEY is empty."
                )
            env_body = f"GRAPHRAG_API_KEY={self._settings.azure_openai_api_key}\n"
        else:
            env_body = "# AZURE_AUTH_MODE=credential — graphrag uses DefaultAzureCredential\n"
        (wd / ".env").write_text(env_body, encoding="utf-8")

        # 5) Write chunk texts as input/*.txt; clear stale text inputs first.
        in_dir = wd / "input"
        in_dir.mkdir(parents=True, exist_ok=True)
        for stale in in_dir.glob("*.txt"):
            stale.unlink()
        # Drop legacy parquets from older runner version.
        for stale in in_dir.glob("*.parquet"):
            stale.unlink()
        n = 0
        for c in chunks:
            (in_dir / f"{c.id}.txt").write_text(c.text or "", encoding="utf-8")
            n += 1

        log.info("graphrag inputs written", extra={
            "event": "graphrag.write_inputs",
            "workdir": str(wd),
            "chunks": n,
            "auth": self._settings.azure_auth_mode.value,
        })
        return wd

    def _overlay_custom_prompts(self, wd: Path) -> int:
        """Copy ``config/graphrag_prompts/*.txt`` over the freshly-init'd defaults.

        Returns the number of templates overlaid. Safe to call when no custom
        templates exist (returns 0).
        """
        # repo root: this file lives at <root>/knowledge_extraction/infrastructure/graphrag/graphrag_runner.py
        repo_root = Path(__file__).resolve().parents[3]
        src_dir = repo_root / "config" / "graphrag_prompts"
        if not src_dir.is_dir():
            return 0
        dst_dir = wd / "prompts"
        dst_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for src in src_dir.glob("*.txt"):
            (dst_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            n += 1
        if n:
            log.info("graphrag custom prompts overlaid", extra={
                "event": "graphrag.prompts.overlay",
                "workdir": str(wd),
                "count": n,
                "source": str(src_dir),
            })
        return n

    def _settings_auth_block(self) -> str:
        """Return the auth fields to splice into a model entry.

        Already indented to align with sibling fields under a model definition
        (4 spaces). The placeholder ``__AUTH_BLOCK__`` is replaced AFTER
        ``textwrap.dedent`` to avoid skewing common-prefix detection.

        - KEY mode  : ``auth_type: api_key`` + ``api_key: ${GRAPHRAG_API_KEY}``
        - CRED mode : ``auth_type: azure_managed_identity`` (no api_key —
                      graphrag rejects setting both; it uses
                      ``DefaultAzureCredential`` via the native LiteLLM path)
        """
        if self._settings.azure_auth_mode == AzureAuthMode.KEY:
            return (
                "    auth_type: api_key\n"
                "    api_key: ${GRAPHRAG_API_KEY}"
            )
        return "    auth_type: azure_managed_identity"

    async def _init(self, wd: Path) -> int:
        # `graphrag init` refuses if files exist; use --force to refresh prompts cleanly.
        # NOTE: graphrag 2.x interactively prompts for chat/embedding model names even
        # when --model/--embedding flags are provided. We feed empty lines on stdin to
        # accept the defaults, and we cap the call at 90s so we fail loudly instead of
        # silently hanging when graphrag adds new prompts in a future release.
        proc = await asyncio.create_subprocess_exec(
            self._exe, "init", "--root", str(wd), "--force",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(
                proc.communicate(input=b"\n" * 10),
                timeout=90,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.error("graphrag init timed out", extra={"event": "graphrag.init.timeout"})
            raise RuntimeError("graphrag init timed out after 90s") from None
        if proc.returncode != 0:
            log.error("graphrag init failed",
                      extra={"err": err.decode("utf-8", "replace")[-2000:]})
        return proc.returncode or 0

    async def index(self, version: OntologyVersion) -> int:
        wd = self.workdir(version)
        # Wipe previous output so re-runs are deterministic.
        for sub in ("output", "cache", "logs"):
            p = wd / sub
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

        # The Azure preflight (validate_config) occasionally returns a transient 401
        # ("Key based authentication is disabled for this resource") even when local
        # auth is enabled. Retry a few times with backoff before giving up.
        rc = 0
        last_err = b""
        for attempt in range(3):
            proc = await asyncio.create_subprocess_exec(
                self._exe, "index", "--root", str(wd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            log.info(
                "graphrag stdout",
                extra={"out": out.decode("utf-8", "replace")[-2000:], "attempt": attempt + 1},
            )
            rc = proc.returncode or 0
            last_err = err
            if rc == 0:
                return 0
            transient = b"Key based authentication is disabled" in err or b"validate_config" in err
            if not transient or attempt == 2:
                break
            log.warning("graphrag transient failure, retrying", extra={"attempt": attempt + 1})
            await asyncio.sleep(2 ** attempt)
        log.error("graphrag failed", extra={"err": last_err.decode("utf-8", "replace")[-2000:]})
        return rc

    # ----------------------------------------------------------------------- #
    # settings.yaml builder                                                    #
    # ----------------------------------------------------------------------- #

    def _azure_settings_yaml(self) -> str:
        """Render ``settings.yaml`` for the installed graphrag schema (2.x —
        ``models:`` dict keyed by model name, nested ``input.storage``).

        Auth mode comes from ``Settings.azure_auth_mode``:
        - ``KEY``        : ``auth_type: api_key`` + ``${GRAPHRAG_API_KEY}``
        - ``CREDENTIAL`` : ``auth_type: azure_managed_identity``
                           (DefaultAzureCredential — required when the Azure
                           resource has key auth disabled). graphrag's LiteLLM
                           wrapper installs ``azure_ad_token_provider`` and
                           skips the api_key channel entirely.
        """
        s = self._settings
        endpoint = s.azure_openai_endpoint.rstrip("/")
        api_version = s.azure_openai_api_version
        chat_model = s.azure_openai_extraction_model
        embed_model = s.azure_openai_embedding_model
        vector_size = _embedding_dimension(embed_model)

        template = textwrap.dedent(f"""\
            # Generated by knowledge_extraction.GraphRagRunner — do not edit manually.
            # Schema: graphrag 2.x (https://microsoft.github.io/graphrag/config/yaml/)
            models:
              default_chat_model:
                type: chat
                model_provider: azure
            __AUTH_BLOCK__
                model: {chat_model}
                deployment_name: {chat_model}
                api_base: {endpoint}
                api_version: "{api_version}"
                model_supports_json: true
                concurrent_requests: 25
                async_mode: threaded
                retry_strategy: exponential_backoff
                max_retries: 6
                max_retry_wait: 10.0
                tokens_per_minute: null
                requests_per_minute: null
              default_embedding_model:
                type: embedding
                model_provider: azure
            __AUTH_BLOCK__
                model: {embed_model}
                deployment_name: {embed_model}
                api_base: {endpoint}
                api_version: "{api_version}"
                concurrent_requests: 25
                async_mode: threaded
                retry_strategy: exponential_backoff
                max_retries: 6
                max_retry_wait: 10.0
                tokens_per_minute: null
                requests_per_minute: null

            input:
              storage:
                type: file
                base_dir: "input"
              file_type: text

            chunks:
              size: 1200
              overlap: 100
              group_by_columns: [id]

            output:
              type: file
              base_dir: "output"

            cache:
              type: file
              base_dir: "cache"

            reporting:
              type: file
              base_dir: "logs"

            vector_store:
              default_vector_store:
                type: lancedb
                db_uri: output/lancedb
                embeddings_schema:
                  entity.description:
                    vector_size: {vector_size}
                  community.full_content:
                    vector_size: {vector_size}
                  text_unit.text:
                    vector_size: {vector_size}

            embed_text:
              model_id: default_embedding_model
              vector_store_id: default_vector_store

            extract_graph:
              model_id: default_chat_model
              prompt: "prompts/extract_graph.txt"
              entity_types: [organization, person, geo, event]
              max_gleanings: 1

            summarize_descriptions:
              model_id: default_chat_model
              prompt: "prompts/summarize_descriptions.txt"
              max_length: 500

            extract_graph_nlp:
              text_analyzer:
                extractor_type: regex_english
              async_mode: threaded

            cluster_graph:
              max_cluster_size: 10

            extract_claims:
              enabled: false
              model_id: default_chat_model
              prompt: "prompts/extract_claims.txt"
              description: "Any claims or facts that could be relevant to information discovery."
              max_gleanings: 1

            community_reports:
              model_id: default_chat_model
              graph_prompt: "prompts/community_report_graph.txt"
              text_prompt: "prompts/community_report_text.txt"
              max_length: 2000
              max_input_length: 8000

            embed_graph:
              enabled: false

            umap:
              enabled: false

            snapshots:
              graphml: false
              embeddings: false

            local_search:
              chat_model_id: default_chat_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/local_search_system_prompt.txt"

            global_search:
              chat_model_id: default_chat_model
              map_prompt: "prompts/global_search_map_system_prompt.txt"
              reduce_prompt: "prompts/global_search_reduce_system_prompt.txt"
              knowledge_prompt: "prompts/global_search_knowledge_system_prompt.txt"

            drift_search:
              chat_model_id: default_chat_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/drift_search_system_prompt.txt"
              reduce_prompt: "prompts/drift_reduce_prompt.txt"

            basic_search:
              chat_model_id: default_chat_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/basic_search_system_prompt.txt"

            workflows:
              - load_input_documents
              - create_base_text_units
              - create_final_documents
              - extract_graph
              - finalize_graph
              - extract_covariates
              - create_communities
              - create_final_text_units
              - create_community_reports
              - generate_text_embeddings
        """)
        return template.replace("__AUTH_BLOCK__", self._settings_auth_block())


# ---------------------------------------------------------------- helpers

# Known embedding-model dimensions (Azure OpenAI). Used to align lancedb
# `vector_size` with the model so writes don't get truncated/rejected.
_EMBED_DIM: dict[str, int] = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
}


def _embedding_dimension(model: str) -> int:
    """Return the embedding vector size for a model name; default to 3072."""
    return _EMBED_DIM.get(model.lower(), 3072)
