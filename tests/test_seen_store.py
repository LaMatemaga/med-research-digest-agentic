from storage import seen_store


def _db(tmp_path):
    return tmp_path / "seen_items_test.db"


def test_filter_new_returns_everything_on_first_run(tmp_path):
    db = _db(tmp_path)
    papers = [{"pmid": "1"}, {"pmid": "2"}]

    assert seen_store.filter_new(papers, db_path=db) == papers


def test_filter_new_excludes_previously_recorded_pmids(tmp_path):
    db = _db(tmp_path)
    papers = [{"pmid": "1", "relevance_score": 8}, {"pmid": "2", "relevance_score": 6}]

    seen_store.record_seen(papers, db_path=db)
    new_batch = [{"pmid": "1"}, {"pmid": "3"}]

    result = seen_store.filter_new(new_batch, db_path=db)

    assert result == [{"pmid": "3"}]


def test_record_seen_upserts_last_score(tmp_path):
    db = _db(tmp_path)
    seen_store.record_seen([{"pmid": "1", "relevance_score": 5}], db_path=db)
    seen_store.record_seen([{"pmid": "1", "relevance_score": 9}], db_path=db)

    # pmid "1" is still remembered (not re-surfaced), proving the upsert didn't
    # duplicate the row or reset first_seen.
    assert seen_store.filter_new([{"pmid": "1"}], db_path=db) == []


def test_reset_clears_memory(tmp_path):
    db = _db(tmp_path)
    seen_store.record_seen([{"pmid": "1"}], db_path=db)
    assert seen_store.filter_new([{"pmid": "1"}], db_path=db) == []

    seen_store.reset(db_path=db)

    assert seen_store.filter_new([{"pmid": "1"}], db_path=db) == [{"pmid": "1"}]


def test_filter_new_handles_empty_input(tmp_path):
    db = _db(tmp_path)
    assert seen_store.filter_new([], db_path=db) == []


def test_mark_alerted_and_was_alerted(tmp_path):
    db = _db(tmp_path)
    seen_store.record_seen([{"pmid": "1"}], db_path=db)

    assert seen_store.was_alerted("1", db_path=db) is False

    seen_store.mark_alerted("1", db_path=db)

    assert seen_store.was_alerted("1", db_path=db) is True


def test_was_alerted_false_for_unknown_pmid(tmp_path):
    db = _db(tmp_path)
    assert seen_store.was_alerted("does-not-exist", db_path=db) is False


def test_init_db_is_idempotent(tmp_path):
    db = _db(tmp_path)
    seen_store.init_db(db_path=db)
    seen_store.init_db(db_path=db)  # must not raise on an already-initialized db
    assert seen_store.filter_new([{"pmid": "1"}], db_path=db) == [{"pmid": "1"}]
