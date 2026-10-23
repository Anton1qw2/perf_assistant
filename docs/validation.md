# Запись проверки выпуска

Дата: 2026-10-23. Версия: 0.1.0. Статус: локальная реализация, deployment validation внешних сервисов ожидается.

Реализованы durable shell, отдельные API/Worker, TaskSpec и snapshots, intake и консультации, scoped диагностика, MCP Streamable HTTP, versioned evidence/report, Git pinning, вопросы и context ledger, bounded planning, hotspot selection/interpretation, reference expert factories, read-only external executor, Web и line TUI.

Проверки включают: дедупликацию и несовпадающие request IDs; stale worker fencing и rollback; restart; one active run; cancellation; cumulative budgets; immutable snapshots; RU/EN non-task isolation; независимую protocol/object authorization; schema reference/discovery limits; zero/false preservation; invalid citations; pinned Git blobs/path bounds; format restart без повторного MCP; credential encryption/binding/scrubbing; Host/Origin/API validation; context confirm/decline и token isolation; external questions; consultation switching; ordered event cursors; browser snapshot ownership и freshness; безопасные Markdown links/images.

Синтетический transport fixture используется только в автоматических тестах. Он не подтверждает совместимость реального профилировщика. Проверен реальный локальный Git на временном репозитории. Web-интерфейс инспектируется через встроенный браузер, включая узкую ширину, состояния диагностики и подготовку задачи. Контраст, focus styles и reduced-motion реализованы; полноценный аудит со screen reader не проводился.

## Открытые пункты

- Реальный MCP endpoint/каталог не предоставлен. После заполнения `.env` выполнить `/check profiler` с известным profile ID; записать версии сервера, проверенный tool schema, reason codes и результат отдельного Run.
- Модель/ключ не предоставлены. Есть fixture-тесты контрактов, но качество RU/EN классификации, фактическая точность интерпретации и совместимость выбранной модели требуют собственного eval. Не считать schema pass оценкой качества модели.
- Внешний executor не предоставлен. Протокол вопросов/результатов проверяется fixtures; гарантии серверной дедупликации требуют проверки конкретного deployment.
- Поддерживается Streamable HTTP, не legacy SSE/stdio; TUI — переносимый line-oriented клиент, не полноэкранный терминальный UI.
- Hotspot convention: точные records из `structuredContent.hotspots`; неизвестная схема не угадывается. Generic allowed-tool calls доступны при настроенной модели, но не доказывают идентичность объекта без semantic adapter.
- Не включены автоматические изменения кода, remote hosting, аутентификация нескольких пользователей, распределённый scheduler и LangGraph. Lifecycle не зависит от конкретного graph engine.
- Для credential store Windows использует DPAPI. В изолированном sandbox DPAPI может быть недоступен; encryption-тесты используют явно предоставленный тестовый Fernet key. Обычный пользовательский запуск DPAPI следует проверить на целевой машине.

Milestone mapping: M1–M2 реализованы; M3–M4 реализованы и проверены контролируемыми fixtures, реальный deployment не валидирован; M5 реализован; M6 ограничен code-owned experts и описанным hotspot convention; M7 — read-only boundary, мутации не включены. Это не утверждение о прохождении всех release acceptance criteria на неизвестном сервере.
