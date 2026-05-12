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

        # 3) Write .env with API key. graphrag 2.7 requires api_key auth for chat models,
        #    so in CREDENTIAL mode we fetch a short-lived bearer token from DefaultAzureCredential.
        api_key = self._resolve_api_key()
        (wd / ".env").write_text(f"GRAPHRAG_API_KEY={api_key}\n", encoding="utf-8")

        # 4) Write chunk texts as input/*.txt; clear stale text inputs first.
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

    def _resolve_api_key(self) -> str:
        """Return an Azure OpenAI API key — either the configured static key, or a
        short-lived AAD bearer token when running in CREDENTIAL mode.

        graphrag 2.7 currently rejects `auth_type: azure_managed_identity` for chat
        models, so we feed the bearer token through the api_key channel. The token
        has a ~1h TTL — fine for a single `graphrag index` run.
        """
        if self._settings.azure_auth_mode == AzureAuthMode.KEY:
            if not self._settings.azure_openai_api_key:
                raise RuntimeError(
                    "AZURE_AUTH_MODE=key but AZURE_OPENAI_API_KEY is empty."
                )
            return self._settings.azure_openai_api_key

        # CREDENTIAL mode: fetch bearer token via DefaultAzureCredential.
        from azure.identity import DefaultAzureCredential

        cred = DefaultAzureCredential()
        token = cred.get_token("https://cognitiveservices.azure.com/.default")
        return token.token

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
        """Render settings.yaml for graphrag 2.x.

        Schema reference: https://microsoft.github.io/graphrag/config/yaml/

        Key 2.x conventions this method honors:
        - `completion_models:` and `embedding_models:` are SEPARATE top-level dicts
          (the unified `models:` block from older graphrag is gone).
        - `auth_method:` (not `auth_type:`); `azure_deployment_name:` (not `deployment_name:`).
        - Workflow refs use `completion_model_id:` / `embedding_model_id:`.
        - `input_storage:` and `output_storage:` are separate top-level sections.
        - `chunking:` (not `chunks:`).
        - For Azure, `model_provider: azure` routes calls through litellm's
          Azure OpenAI adapter.
        - `vector_store.<name>.vector_size` MUST match the embedding model's
          dimension (1536 for ada-002, 3072 for text-embedding-3-large).

        graphrag 2.7 only accepts `api_key` auth for chat/embedding models.
        In CREDENTIAL mode `_resolve_api_key` issues a short-lived AAD bearer
        token and we pass it through the `api_key` field (~1h TTL — fine for
        a single index run).
        """
        s = self._settings
        endpoint = s.azure_openai_endpoint.rstrip("/")
        api_version = s.azure_openai_api_version
        chat_model = s.azure_openai_extraction_model
        embed_model = s.azure_openai_embedding_model
        vector_size = _embedding_dimension(embed_model)

        return textwrap.dedent(f"""\
            # Generated by knowledge_extraction.GraphRagRunner — do not edit manually.
            # Schema: graphrag 2.x (https://microsoft.github.io/graphrag/config/yaml/)
            completion_models:
              default_completion_model:
                model_provider: azure
                model: {chat_model}
                azure_deployment_name: {chat_model}
                api_base: {endpoint}
                api_version: "{api_version}"
                auth_method: api_key
                api_key: ${{GRAPHRAG_API_KEY}}
                retry:
                  type: exponential_backoff
                  max_retries: 6

            embedding_models:
              default_embedding_model:
                model_provider: azure
                model: {embed_model}
                azure_deployment_name: {embed_model}
                api_base: {endpoint}
                api_version: "{api_version}"
                auth_method: api_key
                api_key: ${{GRAPHRAG_API_KEY}}
                retry:
                  type: exponential_backoff
                  max_retries: 6

            input:
              type: text

            input_storage:
              type: file
              base_dir: "input"

            chunking:
              type: tokens
              encoding_model: o200k_base
              size: 1200
              overlap: 100

            output_storage:
              type: file
              base_dir: "output"

            cache:
              type: json
              storage:
                type: file
                base_dir: "cache"

            reporting:
              type: file
              base_dir: "logs"

            vector_store:
              type: lancedb
              db_uri: output/lancedb
              index_schema:
                entity_description:
                  vector_size: {vector_size}
                community_full_content:
                  vector_size: {vector_size}
                text_unit_text:
                  vector_size: {vector_size}

            embed_text:
              embedding_model_id: default_embedding_model

            extract_graph:
              completion_model_id: default_completion_model
              prompt: "prompts/extract_graph.txt"
              entity_types: [organization, person, geo, event]
              max_gleanings: 1

            summarize_descriptions:
              completion_model_id: default_completion_model
              prompt: "prompts/summarize_descriptions.txt"
              max_length: 500

            extract_graph_nlp:
              text_analyzer:
                extractor_type: regex_english

            cluster_graph:
              max_cluster_size: 10

            extract_claims:
              enabled: false
              completion_model_id: default_completion_model
              prompt: "prompts/extract_claims.txt"
              description: "Any claims or facts that could be relevant to information discovery."
              max_gleanings: 1

            community_reports:
              completion_model_id: default_completion_model
              graph_prompt: "prompts/community_report_graph.txt"
              text_prompt: "prompts/community_report_text.txt"
              max_length: 2000
              max_input_length: 8000

            snapshots:
              graphml: true
              embeddings: false

            local_search:
              completion_model_id: default_completion_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/local_search_system_prompt.txt"

            global_search:
              completion_model_id: default_completion_model
              map_prompt: "prompts/global_search_map_system_prompt.txt"
              reduce_prompt: "prompts/global_search_reduce_system_prompt.txt"
              knowledge_prompt: "prompts/global_search_knowledge_system_prompt.txt"

            drift_search:
              completion_model_id: default_completion_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/drift_search_system_prompt.txt"
              reduce_prompt: "prompts/drift_reduce_prompt.txt"

            basic_search:
              completion_model_id: default_completion_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/basic_search_system_prompt.txt"
        """)


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
