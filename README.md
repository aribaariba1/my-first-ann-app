# Smart Kitchen Assistant

An AI Kitchen Assistant Anna App that helps users cook with or without photos:

- **Ingredients photo or list -> Recipe ideas**: Snap or list available ingredients to get 3-5 dishes you can make right now, including what each recipe uses and any small extras needed.
- **Finished meal photo -> Step-by-step tutorial**: Send a photo of an already-made dish to learn how to cook it, complete with a full ingredient list, quantities, equipment, and numbered steps.
- **General cooking chat**: Ask about techniques, substitutions, timings, food safety, or meal planning without attaching any media.
- **Conversation & Photo History**: Conversations and attached photos are saved automatically in the sidebar so you can return to them anytime. Includes per-photo deletion, conversation deletion, and a **Clear all photos** button to free storage while keeping conversation text and recipes.

## Architecture (Host API + Python Executa)

```
bundle/ (iframe UI)
  ├── anna.llm.complete       (Host API: multimodal vision analysis of attached photos)
  ├── anna.storage.*          (Host API: conversation history index & transcripts)
  ├── anna.files.*            (Host API: photo blob storage, with automatic KV fallback in legacy dev mode)
  └── anna.tools.invoke(...)  ──► Python Executa (tool-dev-my-first-anna-app)
                                    ├── chat                    ──► sampling/createMessage (reverse RPC)
                                    ├── summarize_conversation  ──► sampling/createMessage (reverse RPC)
                                    └── ping
```

Why this split? Per Anna's specification, Executa `sampling/createMessage` is text-only (`{type: "text"}`), while `anna.llm.complete` in the Host API supports multimodal `{type: "image", data, mimeType}` blocks. The frontend converts attached photos into structured JSON (`ingredients` vs `dish`), then invokes the Python Executa which performs the culinary reasoning and conversation summarization via reverse Sampling.

## Project Structure

- `app.json` — App metadata (`Smart Kitchen Assistant`, tagline, description, category).
- `manifest.json` — Schema v2 manifest declaring `tool-dev-my-first-anna-app`, `host_capabilities` (`llm.sample`, `llm.complete`, `aps.kv`, `aps.files`), and `ui.host_api` method grants.
- `bundle/index.html`, `bundle/style.css`, `bundle/app.js`, `bundle/media.js`, `bundle/markdown.js` — Static SPA frontend bundle (zero build step required).
- `executas/my-first-anna-app/`
  - `my_first_anna_app_plugin.py` — Python stdio JSON-RPC 2.0 plugin (protocol v2 capability negotiation + reverse `sampling/createMessage`).
  - `executa.json` — Executa publishing metadata and `local` / `binary` distribution profiles.
  - `pyproject.toml` — Python package and console script definition (`tool-dev-my-first-anna-app`).
  - `tests/test_plugin.py` — Pytest suite testing stdio lifecycle, `describe`, `ping`, `chat`, `summarize_conversation`, and error propagation.
- `fixtures/happy-path.jsonl` — Mock LLM fixture for offline harness testing.

## Validation & Local Development

### 1. Validate Manifest & ACLs
```bash
anna-app validate
anna-app validate --strict
```

### 2. Run Python Executa Unit Tests
```bash
cd executas/my-first-anna-app
uv sync
uv run pytest
```

### 3. Run the Local Dev Harness
With real Anna LLM (requires `anna-app login` and an active account):
```bash
anna-app dev --slug my-first-ann-app
```

Or offline with canned mock LLM responses:
```bash
anna-app dev --mock-llm fixtures/happy-path.jsonl --slug my-first-ann-app
```
Then open `http://localhost:5180/` in your browser.

## Packaging & Publishing

1. **Binary packaging (optional for standalone distribution)**:
   Build a single-file executable per target platform with PyInstaller (`pyinstaller --onefile my_first_anna_app_plugin.py --name tool-dev-my-first-anna-app`), upload archives to your release host, and populate `distribution.profiles.binary.binary_urls` in `executas/my-first-anna-app/executa.json`.
2. **Publish the App**:
   ```bash
   anna-app publish
   ```
# my-first-ann-app
