-- 018_blank_users_api_key.sql — SEC: обнуление plaintext users.api_key
--
-- Шаг 2 миграции ключей пользователей. 017 добавил api_key_hash, заполнил его
-- и сделал единственным путём поиска; все писатели (signup, OAuth, rotate,
-- seed) уже кладут '' в api_key и только дайджест. Значит столбец можно
-- перестать держать живым секретом.
--
-- api_key объявлен NOT NULL без DEFAULT, поэтому «пусто» — это '', а не NULL.
--
-- Идемпотентно: WHERE пропускает уже обнулённые строки, повторный прогон
-- (run_migrations.py или ручной apply) не меняет ни одной строки.

-- Guard: строка без бэкфилла (есть plaintext, нет хэша) означает, что 017 не
-- доехал — тогда ключ стал бы невалидным без замены. Ничего не трогаем.
DO $$
DECLARE
    orphans bigint;
BEGIN
    SELECT count(*) INTO orphans
      FROM users
     WHERE coalesce(api_key, '') <> ''
       AND coalesce(api_key_hash, '') = '';

    IF orphans > 0 THEN
        RAISE EXCEPTION
            'users: % строк(и) с непустым api_key и пустым api_key_hash — сначала бэкфилл (017), обнуление отменено (018).',
            orphans;
    END IF;
END $$;

UPDATE users
   SET api_key = ''
 WHERE coalesce(api_key, '') <> '';

-- Пост-проверка в той же транзакции: тихого частичного обнуления быть не должно.
DO $$
DECLARE
    remaining bigint;
BEGIN
    SELECT count(*) INTO remaining
      FROM users
     WHERE coalesce(api_key, '') <> '';

    IF remaining > 0 THEN
        RAISE EXCEPTION 'users: после 018 осталось % строк с непустым api_key (018).', remaining;
    END IF;
END $$;
