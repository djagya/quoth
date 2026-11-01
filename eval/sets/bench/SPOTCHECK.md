# Bench set: owner spot check

40 scored rows drawn at random (seed 20261101), stratified by row type (single 12, negatives 6, groups 6, paraphrase 3, followup 1, abstain:not_in_vault 4, abstain:false_premise 4, abstain:near_miss 4),
alternating English and Russian inside each stratum. For each row: is the expected note
right, and is the question something a person would ask? Mark a row `ok`, `relabel: ...`
or `drop` in the last column.

Expected notes are vault paths under `eval/bench/vault/`; `A or B` means either note
answers; `A and B` means both are needed (all-of groups). Negatives are look-alike notes that
must not be injected.

| # | id | stratum | lang | question | expected | negatives | verdict |
|---|---|---|---|---|---|---|---|
| 1 | `career-05` | single | en | When is the promotion packet due? | `WORK/1-1 notes with Hanne.md` | — | |
| 2 | `ledgerline-26` | single | ru | Что случилось на работе в начале июня? | `WORK/Projects/2026-06-12 Ledgerline postmortem.md` | — | |
| 3 | `cat-13` | single | en | Who's the backup if the neighbour can't feed the cat? | `PETS/Кто кормит Бублика.md` | — | |
| 4 | `weeknight-17` | single | ru | рис пропорция рисоварка | `KNOWLEDGE/Recipes/Meal prep Sundays.md` | — | |
| 5 | `transport-15` | single | en | What did I sell last March? | `DECISIONS/2026-03-01 Sell the car.md` or `FINANCE/Budget 2026.md` | — | |
| 6 | `tech-12` | single | ru | Что случилось с ноутбуком в конце августа? | `JOURNAL/2026-08-30.md` or `TECH/Laptop setup.md` | — | |
| 7 | `health-03` | single | en | When should I start taking cetirizine? | `HEALTH/Allergies and medication.md` | — | |
| 8 | `health-06` | single | ru | Когда следующий чекап у терапевта? | `HEALTH/2026-02-18 Checkup.md` | — | |
| 9 | `apartment-13` | single | en | router admin password where | `HOME/Utilities and internet.md` | — | |
| 10 | `piano-06` | single | ru | Что у меня плохо получается на пианино? | `HOBBIES/Пианино — дневник занятий.md` | — | |
| 11 | `portugal-16` | single | en | portugal next trip ideas | `TRAVEL/Portugal — ideas for next time.md` | — | |
| 12 | `hiking-05` | single | ru | Когда нужно бронировать хижины в Доломитах? | `TRAVEL/2025-08-24 Dolomites hut trek.md` | — | |
| 13 | `tidepool-06` | negatives | en | What did the original Tidepool plan use for storage? | `PROJECTS/Tidepool v1 plan.md` | `PROJECTS/Tidepool v2 plan.md` | |
| 14 | `baking-04` | negatives | ru | При какой температуре печь ржаной хлеб? | `KNOWLEDGE/Recipes/Rye sourdough.md` | `KNOWLEDGE/Recipes/Focaccia.md` | |
| 15 | `oncall-08` | negatives | en | How long did the DNS outage last? | `WORK/Incidents/2025-11-20 DNS outage.md` | `WORK/Incidents/2026-07-08 Kestrel queue backlog.md` | |
| 16 | `piano-04` | negatives | ru | Что я сейчас разучиваю на пианино? | `HOBBIES/Пианино — дневник занятий.md` | `HOBBIES/Пианино — что хочу выучить.md` | |
| 17 | `portugal-13` | negatives | en | Where did I stay in Alfama and how much was it a night? | `TRAVEL/2026-05-16 Lisbon.md` | `TRAVEL/2025-04-20 Porto.md` | |
| 18 | `finance-04` | negatives | ru | Какая цель у подушки безопасности? | `FINANCE/Budget 2026.md` | `FINANCE/Budget 2025.md` | |
| 19 | `finance-14` | groups | en | How did my monthly ETF contribution change from 2025 to 2026? | `FINANCE/Budget 2025.md` **and** `FINANCE/Budget 2026.md` | — | |
| 20 | `running-19` | groups | ru | Как колено вело себя на полумарафоне и какие упражнения я для него делал? | `JOURNAL/2026-10-11.md` **and** `HEALTH/Knee physio.md` | — | |
| 21 | `portugal-20` | groups | en | What was my DataTide talk about, and who approved the conference budget for it? | `TRAVEL/2026-05-16 Lisbon.md` **and** `WORK/1-1 notes with Hanne.md` | — | |
| 22 | `portugal-19` | groups | ru | Сколько дней длилась каждая из двух поездок в Португалию? | `TRAVEL/2025-04-20 Porto.md` **and** `TRAVEL/2026-05-16 Lisbon.md` | — | |
| 23 | `ledgerline-19` | groups | en | What's left to do on Ledgerline after the migration? | `WORK/Projects/Ledgerline migration.md` **and** `WORK/Projects/Ledgerline decommission.md` | — | |
| 24 | `journal-14` | groups | ru | Как у меня с обещанием про телефон перед сном и что это дало для сна? | `JOURNAL/2026-01-01.md` **and** `HEALTH/Сон.md` | — | |
| 25 | `tech-02` | paraphrase | en | restic schedule | `TECH/Backup setup.md` | — | |
| 26 | `friends-07` | paraphrase | ru | У Джоны есть аллергия на что-нибудь? | `PEOPLE/Jonah Pelt.md` or `KNOWLEDGE/Recipes/Chickpea spinach curry.md` | — | |
| 27 | `piano-02` | paraphrase | en | Which digital piano did I buy? | `HOBBIES/Пианино — дневник занятий.md` | — | |
| 28 | `portugal-21` | followup | en | (after: Remind me about the Porto trip. / You went with Ilse for five days in April 2025.) Where did we stay? | `TRAVEL/2025-04-20 Porto.md` | `TRAVEL/2026-05-16 Lisbon.md` | |
| 29 | `friends-17` | abstain:not_in_vault | en | What does Mark do for a living? | none (`not_in_vault`) | — | |
| 30 | `career-17` | abstain:not_in_vault | ru | Когда у меня отпуск в декабре? | none (`not_in_vault`) | — | |
| 31 | `family-24` | abstain:not_in_vault | en | What's my aunt's phone number? | none (`not_in_vault`) | — | |
| 32 | `books-21` | abstain:not_in_vault | ru | Что я подарил Даше на день рождения? | none (`not_in_vault`) | — | |
| 33 | `ledgerline-22` | abstain:false_premise | en | Why did we migrate Ledgerline to Oracle? | none (`false_premise`) | — | |
| 34 | `books-17` | abstain:false_premise | ru | Почему я бросил «Пикник на обочине»? | none (`false_premise`) | — | |
| 35 | `friends-18` | abstain:false_premise | en | Why didn't I go to Ilse's wedding? | none (`false_premise`) | — | |
| 36 | `running-27` | abstain:false_premise | ru | Почему я не побежал полумарафон в октябре? | none (`false_premise`) | — | |
| 37 | `career-18` | abstain:near_miss | en | What did my 2024 performance review say? | none (`near_miss`) | `WORK/Performance review 2025.md` | |
| 38 | `transport-24` | abstain:near_miss | ru | Сколько стоило обслуживание велосипеда в 2025 году? | none (`near_miss`) | `HOME/Bike maintenance.md` | |
| 39 | `baking-20` | abstain:near_miss | en | What hydration do I use for pizza dough? | none (`near_miss`) | `KNOWLEDGE/Recipes/Focaccia.md` | |
| 40 | `running-28` | abstain:near_miss | ru | Какие упражнения для спины дал физиотерапевт? | none (`near_miss`) | `HEALTH/Knee physio.md` | |
