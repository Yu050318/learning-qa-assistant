# Stage 8 isolated Docker acceptance

The isolated deployment checks below passed. The database service was subsequently restored by the user; a fresh business backup was restored and checked before migration. Native API/frontend and all three Workers now pass preflight and HTTP readiness. [Business preservation and deployment evidence](stage8-deployment.json). The business runtime enables approvals and keeps Sandbox opt-in/default off; it does not have an eligible Docker execution driver. Full Sandbox execution remains available in the separately validated Docker development deployment. Stop after stage 8; do not start stage 9.

## Verified implementation

- Stock Docker API, frontend and independent ingestion/Run/evaluation Workers run against disposable PostgreSQL and Milvus namespaces. 107 Python source files in the API image exactly match the working tree. Only the trusted Run Worker has the Docker CLI/socket. Workload image is fixed by immutable SHA256; production rootless remains the default gate. Docker Desktop acceptance uses an explicit development rootful exception.
- Real DeepSeek mixed flow called public.echo over HTTPS and Sandbox in one Run. The Run froze v4 declarations before the catalog changed to v5. It paused for clarification and resumed the same Run. SQL confirms generation 2, one approval, exactly two non-readonly receipts, each attempted once, and one SHA256-verified artifact containing `42`.
- A second flow was submitted in the browser against v5. Blank public query disabled approval; the edited public query alone reached the echo service. Refresh restored the waiting Run; supplement completed the same Run. SQL confirms one approval, two single-attempt receipts and one file. Private test markers were absent from the actual HTTP echo.
- Real ingestion, Dashscope embeddings and Milvus retrieval produced six cited leaf sources and one parent context for a fictional warranty handbook. Browser trace displays source excerpts and budgets; parent-only context has no independent citation button. This fictional fixture is connectivity evidence, not an enterprise quality benchmark.
- The entire Run Worker was SIGKILLed while an actual Sandbox workload was running. A new owner took over after lease expiry and reported TOOL_OUTCOME_UNCERTAIN, with one attempt and no artifact/reexecution. Its namespace-limited reaper removed the actual orphan after the grace period.
- Reader access to another account's trace and artifacts returns 404. Prior stage8 suites cover cancellation, role/workspace changes, bounded artifacts, safe text/PNG previews and active-content attachment handling.

## Regressions and limitations

Real model acceptance exposed parallel discovery ordering deadlock; discovery tools now receive ToolRuntime and two regression cases cover discovery plus Sandbox in the same tool batch. Clarification citation errors prompted explicit clarification instructions and bounded validation-reason feedback, while strict citation validation remains. Another failed model run confused an HTTP Tool with a Skill; the acceptance Skill was explicitly versioned with concrete tool instructions. Failed exploratory Runs remain retained; these checks do not establish general model reliability.

Complete backend: **775 passed, 52 skipped, 38 subtests**. Frontend: **45 tests** and production build passed. PostgreSQL artifact/locking suites: **4 passed** in the earlier stage8 evidence. Stage5 enterprise A/B failure reports are unchanged.

The last verified stage7 backup was independently restored to a disposable database and upgraded from 0008 to 0010. Project counts, roles and password hashes were preserved; artifact table starts empty; scratch database was removed. This is an old-backup rehearsal, not a fresh current business backup. The earlier SSL failure was subsequently resolved; business migration and its explicit single configuration change are recorded separately in stage8-deployment.json.

## Evidence

- [Machine-readable deployment and SQL assertions](stage8-docker-acceptance.json)
- [Old-backup restore rehearsal](stage8-restore-rehearsal.json)
- [Business connection blocker](stage8-business-connection.json)
- [Browser approval](../screenshots/stage8-docker-public-input-approval.png)
- [Waiting after real tools](../screenshots/stage8-docker-hitl-paused.png)
- [Same Run resumed](../screenshots/stage8-docker-hitl-completed.png)
- [RAG context](../screenshots/stage8-docker-rag-context.png)
- [Cited leaf](../screenshots/stage8-docker-rag-leaf.png)
