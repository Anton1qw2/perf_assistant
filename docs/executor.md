# API, вопросы и внешний исполнитель

OpenAPI находится в `/openapi.json`. Все изменяющие команды разговора имеют `request_id`. Повтор того же ID и тела возвращает прежний результат; другое тело с тем же ID — 409. Исключение — создание разговора: оно не имеет request_id. Для credential-команд в дедупликации сохраняется только факт передачи токена, без его значения/хеша.

Основные маршруты:

| Метод | Путь | Назначение |
|---|---|---|
| POST/GET | `/api/conversations` | Создать / перечислить |
| GET | `/api/conversations/{id}` | Согласованный snapshot + event cursor |
| POST | `…/messages` | Intake (`text`) |
| PATCH | `…/task` | TaskPatch с expected_version |
| PUT | `…/settings` | Настройки с expected_revision; отдельный token field |
| POST | `…/runs` | Явный Run |
| POST | `…/cancel` | Отменить активный запуск |
| POST | `…/questions/{qid}/answer` | Коррелированный ответ |
| POST | `…/runs/{rid}/context` | Предложение смены контекста; revision текущей области |
| POST/GET | `…/connection-checks` | Поставить проверку / прочитать сохранённые результаты |
| GET | `…/events?after=N` | SSE replay; также Last-Event-ID |
| GET | `…/evidence/{eid}` | Оригинальный payload и provenance в границах разговора |

События передают IDs и безопасные summary, после них клиент перечитывает snapshot. Нельзя использовать arrival order без server event cursor. Клиенты защищают request/SSE ownership при переключении разговоров. Drafts сообщений в Web сохраняются per-conversation в sessionStorage; credentials никогда туда не записываются.

## ExecutorGateway

Приложение включает HTTP-адаптер для **read-only** инфраструктуры. `PA_EXECUTOR_URL` — base HTTPS URL; `PA_EXECUTOR_TOKEN` — отдельный destination-bound token. В задаче указывается `options.executor=external`.

POST `/step`:

```json
{
  "schema_version": 1,
  "run_id": "stable-run-id",
  "operation_id": "stable-run-id:0",
  "mode": "read_only",
  "task_snapshot": {},
  "checkpoint": {},
  "answer": null
}
```

`Idempotency-Key` совпадает с operation_id. Ответ содержит `event` (`progress`, `question`, `result`) и JSON-объект `checkpoint`. Для question передайте `prompt`. После ответа клиента следующий шаг получает `answer: {question_id, answer}`. Run context confirmations принадлежат хосту, не внешнему исполнителю.

POST `/cancel`: `{run_id, operation_id: "<run>:cancel"}`. Исполнитель обязан поддерживать идемпотентность и cooperative cancellation; локальная отмена запрещает commit любого опоздавшего шага.

Отчёт хранит внешний результат как evidence. Claim внешнего сервера об ускорении не превращается автоматически в measured improvement или correctness. Для включения изменений кода нужен отдельный versioned proposal/approval протокол с точным scope, before/after measurements, correctness checks и серверной дедупликацией побочных эффектов. В текущем выпуске мутации отключены намеренно, включая диагностические вызовы.
