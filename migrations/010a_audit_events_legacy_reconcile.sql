-- 010a_audit_events_legacy_reconcile.sql (reconciling, v1)
-- G-AUDIT-DDL-DRIFT (R2b/R2c/R2e): реконсиляция ИСТОРИЧЕСКОЙ формы `audit_events`
-- ДО 011. 011 и 012 уже применены на проде (schema_migrations) — их текст заморожен,
-- поэтому исправление живёт отдельным файлом, который встаёт между 010 и 011
-- (тот же приём, что 015a между 015 и 016).
--
-- Почему это нужно. Историческая форма из рантайм-бутстрапа
-- (docs/DECISIONOS-TRANSFORMATION.md §2.1) объявляет:
--     id         UUID PRIMARY KEY DEFAULT gen_random_uuid()
--     entity_id  UUID NOT NULL
--     data       JSONB NOT NULL DEFAULT '{}'
--     created_at TIMESTAMPTZ DEFAULT now()
-- На такой БД 011 падает fail-closed на сверке сигнатуры (`id`/`entity_id`: uuid != text),
-- и цепочка миграций встаёт целиком: частичный UNIQUE и дедуп не появляются, а писатель
-- db_adapter не может вставить безключевые события (`entity_id='unknown'` — не uuid).
-- Повторный прогон при этом не воспроизводим: результат зависит от того, откуда взялась
-- таблица, а не от состояния схемы.
--
-- Правила файла:
--   * сверка типов — по `udt_name` (каноническое внутреннее имя типа), а не по
--     `data_type = udt_name = <literal>`; проверяются ВСЕ шесть базовых колонок,
--     включая `data` (011 сверяет data только как jsonb, 012 не сверяет базовые вовсе);
--   * конвертация — только из явного allowlist исторических форм, без потери данных:
--       uuid -> text   через `col::text` (инъективно, канонический вид uuid);
--       text/json -> jsonb (значения проверяются на валидность JSON ДО ALTER);
--   * DEFAULT обрабатывается корректно: uuid-дефолт (`gen_random_uuid()` /
--     `uuid_generate_v4()`) снимается ПЕРЕД ALTER TYPE — иначе PG не сможет привести
--     дефолт к text, — и возвращается как `<expr>::text`; любой другой неожиданный
--     дефолт — отказ, а не молчаливая потеря семантики;
--   * NOT NULL сохраняется (ALTER TYPE его не снимает);
--   * таблицы нет — no-op: 011 создаст каноническую форму сама;
--   * повторный прогон — 0 изменений (idempotent);
--   * всё, что вне allowlist (тип колонки, отсутствующая базовая колонка, лишняя
--     NOT NULL-без-default колонка, PK не ровно (id)) — fail-closed ДО первой мутации.
--
-- Транзакцией владеет scripts/run_migrations.py: BEGIN/COMMIT здесь нет, а весь
-- файл — один DO-блок, то есть одна атомарная инструкция.

DO $$
DECLARE
    v_typ       text;
    v_missing   text;
    v_extra     text;
    v_default   text;
    v_readd     text;
    v_col       text;
    v_bad       text;
    v_pk_bad    boolean;
    v_changed   text := '';
BEGIN
    -- (0) Таблицы нет — не наша работа: 011 создаст каноническую форму (data JSONB).
    IF to_regclass('public.audit_events') IS NULL THEN
        RAISE NOTICE 'audit_events legacy reconcile (010a): таблицы нет — no-op, канон создаст 011';
        RETURN;
    END IF;

    -- (1) Все шесть базовых колонок обязаны существовать: отсутствие любой — отказ
    --     до мутации (иначе ALTER по части схемы оставил бы её половинчатой).
    SELECT string_agg(e.c, ', ' ORDER BY e.c) INTO v_missing
    FROM (VALUES ('id'), ('tenant_id'), ('event_type'), ('entity_type'), ('entity_id'), ('data')) AS e(c)
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns a
        WHERE a.table_schema = 'public' AND a.table_name = 'audit_events'
          AND a.column_name = e.c
    );
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION
            'audit_events legacy reconcile (010a): отсутствуют базовые колонки [%] — схема не наша, отказ до мутации',
            v_missing;
    END IF;

    -- (2) Лишняя NOT NULL-колонка без default сломает шестиколоночный INSERT из
    --     db_adapter — значит, 011 всё равно откажет. Отказываемся здесь, ДО мутации,
    --     чтобы не оставить применённую конвертацию под заведомо падающий 011.
    --     Автозаполняемые (identity / generated) не считаются обязательными — правило
    --     из 012 (тред :69).
    SELECT string_agg(column_name || '(' || data_type || ')', ', ' ORDER BY column_name)
      INTO v_extra
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'audit_events'
       AND column_name NOT IN ('id', 'tenant_id', 'event_type', 'entity_type', 'entity_id', 'data')
       AND is_nullable = 'NO'
       AND column_default IS NULL
       AND coalesce(is_identity, 'NO') <> 'YES'
       AND coalesce(is_generated, 'NEVER') <> 'ALWAYS';
    IF v_extra IS NOT NULL THEN
        RAISE EXCEPTION
            'audit_events legacy reconcile (010a): обязательные дополнительные колонки [%] — 011 откажет, отказ до мутации',
            v_extra;
    END IF;

    -- (3) PK ровно (id): составной PK (id, tenant_id) не пройдёт 011.
    SELECT NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public' AND tc.table_name = 'audit_events'
          AND tc.constraint_type = 'PRIMARY KEY'
        GROUP BY tc.constraint_name
        HAVING count(kcu.column_name) = 1 AND bool_and(kcu.column_name = 'id')
    ) INTO v_pk_bad;
    IF v_pk_bad THEN
        RAISE EXCEPTION
            'audit_events legacy reconcile (010a): PRIMARY KEY не ровно (id) — 011 откажет, отказ до мутации';
    END IF;

    -- (4) Пять «ключевых» колонок: text (канон) либо uuid (историческая форма, lossless).
    FOREACH v_col IN ARRAY ARRAY['id', 'tenant_id', 'event_type', 'entity_type', 'entity_id'] LOOP
        SELECT a.udt_name INTO v_typ
          FROM information_schema.columns a
         WHERE a.table_schema = 'public' AND a.table_name = 'audit_events'
           AND a.column_name = v_col;
        CONTINUE WHEN v_typ = 'text';

        IF v_typ <> 'uuid' THEN
            RAISE EXCEPTION
                'audit_events legacy reconcile (010a): колонка % имеет неожиданный тип [%] (allowlist: text | uuid) — отказ до мутации',
                v_col, v_typ;
        END IF;

        -- DEFAULT: uuid-генератор снимается перед ALTER TYPE и возвращается text-safe.
        SELECT pg_get_expr(d.adbin, d.adrelid) INTO v_default
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE c.oid = 'public.audit_events'::regclass
           AND a.attname = v_col AND a.attnum > 0;

        v_readd := NULL;
        IF v_default IS NOT NULL THEN
            IF v_default NOT IN ('gen_random_uuid()', 'uuid_generate_v4()') THEN
                RAISE EXCEPTION
                    'audit_events legacy reconcile (010a): неожиданный DEFAULT [%] у колонки % — семантику не угадываем, отказ до мутации',
                    v_default, v_col;
            END IF;
            EXECUTE format('ALTER TABLE audit_events ALTER COLUMN %I DROP DEFAULT', v_col);
            v_readd := v_default || '::text';
        END IF;

        EXECUTE format('ALTER TABLE audit_events ALTER COLUMN %I TYPE text USING %I::text', v_col, v_col);

        IF v_readd IS NOT NULL THEN
            EXECUTE format('ALTER TABLE audit_events ALTER COLUMN %I SET DEFAULT %s', v_col, v_readd);
        END IF;

        v_changed := v_changed || format('%s:uuid->text ', v_col);
    END LOOP;

    -- (5) data: jsonb — канон (no-op); text/json — конвертация с проверкой JSON.
    SELECT a.udt_name INTO v_typ
      FROM information_schema.columns a
     WHERE a.table_schema = 'public' AND a.table_name = 'audit_events'
       AND a.column_name = 'data';

    IF v_typ IN ('text', 'json') THEN
        BEGIN
            -- Приведение каждого не-NULL значения: первый невалидный JSON прерывает
            -- блок, и мы отказываемся ДО ALTER (весь DO-блок атомарен — мутаций не будет).
            PERFORM 1 FROM audit_events WHERE data IS NOT NULL AND (data::jsonb) IS NOT NULL;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION
                'audit_events legacy reconcile (010a): data содержит невалидный JSON — конвертация в jsonb отменена, схема не изменена (SQLSTATE %)',
                SQLSTATE;
        END;
        EXECUTE 'ALTER TABLE audit_events ALTER COLUMN data TYPE jsonb USING data::jsonb';
        v_changed := v_changed || format('data:%s->jsonb ', v_typ);
    ELSIF v_typ <> 'jsonb' THEN
        RAISE EXCEPTION
            'audit_events legacy reconcile (010a): колонка data имеет неожиданный тип [%] (allowlist: jsonb | text | json) — отказ до мутации',
            v_typ;
    END IF;

    -- (6) Пост-проверка: после мутаций схема обязана быть канонической (та же сигнатура,
    --     которую требует 011). Иначе — отказ, а не «почти сконвертировали».
    SELECT string_agg(e.c || '(' || COALESCE(a.udt_name, '<нет>') || ')', ', ' ORDER BY e.c)
      INTO v_bad
      FROM (VALUES ('id'), ('tenant_id'), ('event_type'), ('entity_type'), ('entity_id'), ('data')) AS e(c)
      LEFT JOIN information_schema.columns a
        ON a.table_schema = 'public' AND a.table_name = 'audit_events' AND a.column_name = e.c
     WHERE a.column_name IS NULL
        OR a.udt_name <> CASE WHEN e.c = 'data' THEN 'jsonb' ELSE 'text' END;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'audit_events legacy reconcile (010a): пост-проверка не прошла: %', v_bad;
    END IF;

    IF v_changed = '' THEN
        RAISE NOTICE 'audit_events legacy reconcile (010a): схема уже каноническая — 0 изменений (idempotent)';
    ELSE
        RAISE NOTICE 'audit_events legacy reconcile (010a): конвертировано [%] — канон 011 достигнут', trim(v_changed);
    END IF;
END $$;
