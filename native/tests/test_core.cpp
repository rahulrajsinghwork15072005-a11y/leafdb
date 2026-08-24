// Minimal zero-dependency test harness for leafdb-core.
#include "leafdb_core.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <random>
#include <string>

using namespace ldb;

static int failures = 0;
#define CHECK(cond)                                                        \
    do {                                                                   \
        if (!(cond)) {                                                     \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);    \
            ++failures;                                                    \
        }                                                                  \
    } while (0)

static Bytes val(uint64_t v, size_t width = 8) {
    Bytes b(width, 0);
    for (size_t i = 0; i < width && i < 8; ++i)
        b[i] = uint8_t(v >> (8 * i));
    return b;
}

static void remove_file(const std::string& p) { std::remove(p.c_str()); }

static void test_crc_vector() {
    const char* s = "123456789";
    CHECK(crc32(reinterpret_cast<const uint8_t*>(s), 9) == 0xCBF43926u);
}

static void test_insert_search_basic() {
    remove_file("ldb_t1.db");
    Pager pager("ldb_t1.db", 4096);
    BTree tree(pager, 4096);
    uint32_t root = tree.create_root();
    Bytes got;
    CHECK(!tree.search(root, 1, got));
    root = tree.insert(root, 10, val(99));
    CHECK(tree.search(root, 10, got));
    CHECK(got == val(99));
    CHECK(!tree.search(root, 11, got));
}

static void test_duplicate_throws() {
    remove_file("ldb_t2.db");
    Pager pager("ldb_t2.db", 4096);
    BTree tree(pager, 4096);
    uint32_t root = tree.create_root();
    root = tree.insert(root, 5, val(1));
    bool threw = false;
    try {
        root = tree.insert(root, 5, val(2));
    } catch (DuplicateKeyError&) {
        threw = true;
    }
    CHECK(threw);
}

static void test_shuffled_sorted_range() {
    remove_file("ldb_t3.db");
    Pager pager("ldb_t3.db", 4096);
    BTree tree(pager, 4096);
    uint32_t root = tree.create_root();
    std::vector<int> keys(500);
    for (int i = 0; i < 500; ++i) keys[i] = i;
    std::mt19937 rng(7);
    std::shuffle(keys.begin(), keys.end(), rng);
    for (int k : keys) root = tree.insert(root, k, val(k));

    auto all = tree.range_scan(root, std::nullopt, std::nullopt);
    CHECK(all.size() == 500);
    bool sorted = true;
    for (size_t i = 0; i < all.size(); ++i)
        if (all[i].first != int64_t(i)) sorted = false;
    CHECK(sorted);

    auto mid = tree.range_scan(root, 100, 119);
    CHECK(mid.size() == 20);
    CHECK(mid.front().first == 100 && mid.back().first == 119);

    auto tail = tree.range_scan(root, 490, std::nullopt);
    CHECK(tail.size() == 10);
}

static void test_upsert_and_delete() {
    remove_file("ldb_t4.db");
    Pager pager("ldb_t4.db", 4096);
    BTree tree(pager, 4096);
    uint32_t root = tree.create_root();
    root = tree.upsert(root, 7, val(1));
    root = tree.upsert(root, 8, val(2));
    root = tree.upsert(root, 7, val(111));
    Bytes v;
    CHECK(tree.search(root, 7, v) && v == val(111));

    bool found = false;
    root = tree.remove(root, 15, found);
    CHECK(!found);
    root = tree.remove(root, 7, found);
    CHECK(found);
    CHECK(!tree.search(root, 7, v));
}

static void test_deep_tree_with_mass_deletes() {
    const uint32_t PS = 160;
    remove_file("ldb_t5.db");
    Pager pager("ldb_t5.db", PS);
    BTree tree(pager, PS);
    uint32_t root = tree.create_root();
    for (int k = 800; k >= 1; --k) root = tree.insert(root, k, val(k % 256, 4));
    BTreeStats full = tree.stats(root);
    CHECK(full.depth >= 3);
    CHECK(tree.check(root).ok);
    int deleted = 0;
    for (int k = 4; k <= 800; k += 4) {
        bool f = false;
        root = tree.remove(root, k, f);
        if (f) ++deleted;
    }
    CHECK(deleted == 200);
    CheckResult cr = tree.check(root);
    CHECK(cr.ok);
    CHECK(cr.keys == 600);
    auto remaining = tree.range_scan(root, std::nullopt, std::nullopt);
    CHECK(remaining.size() == 600);
}

static void test_oracle_random_ops() {
    for (unsigned seed : {1u, 2u, 3u}) {
        const uint32_t PS = 160;
        remove_file("ldb_oracle.db");
        Pager pager("ldb_oracle.db", PS);
        BTree tree(pager, PS);
        uint32_t root = tree.create_root();
        std::mt19937 rng(seed * 7919);
        std::map<int64_t, Bytes> oracle;
        for (int step = 0; step < 1500; ++step) {
            int64_t k = rng() % 401;
            double op = std::generate_canonical<double, 24>(rng);
            if (op < 0.60) {
                Bytes v = val(rng() % 1000);
                try {
                    root = tree.insert(root, k, v);
                    oracle[k] = v;
                } catch (DuplicateKeyError&) {}
            } else if (op < 0.85) {
                bool f = false;
                root = tree.remove(root, k, f);
                CHECK(f == (oracle.count(k) > 0));
                oracle.erase(k);
            } else {
                Bytes got;
                bool hit = tree.search(root, k, got);
                auto it = oracle.find(k);
                CHECK(hit == (it != oracle.end()));
                if (hit && got != it->second) CHECK(false);
            }
        }
        CheckResult cr = tree.check(root);
        CHECK(cr.ok);
        auto final_rows = tree.range_scan(root, std::nullopt, std::nullopt);
        CHECK(final_rows.size() == oracle.size());
        for (auto& [k, v] : final_rows) {
            CHECK(oracle.count(k) == 1);
            CHECK(oracle[k] == v);
        }
    }
}

static void test_wal_recover_and_torn_tail() {
    std::string dbp = "ldb_wal.db";
    std::string walp = dbp + ".wal";
    remove_file(dbp);
    remove_file(walp);
    {
        std::ofstream c(dbp.c_str(), std::ios::binary);
    }
    {
        WAL wal(walp);
        std::map<uint32_t, Bytes> batch;
        batch[1] = Bytes(4096, 'A');
        wal.append_batch(batch, 2);
        FILE* f = std::fopen(walp.c_str(), "ab");
        std::fputc(0x00, f);
        std::fputc(0x00, f);
        std::fputc(0x00, f);
        std::fclose(f);
    }
    uint32_t applied = wal_recover(walp, dbp);
    CHECK(applied == 1);

    Pager pager(dbp, 4096);
    Bytes page1;
    pager.get(1, page1);
    CHECK(page1[0] == 'A' && page1[1] == 'A');
    CHECK(pager.num_pages() >= 2);
}

static void test_value_too_large_rejected() {
    remove_file("ldb_big.db");
    Pager pager("ldb_big.db", 4096);
    BTree tree(pager, 4096);
    uint32_t root = tree.create_root();
    bool threw = false;
    try {
        root = tree.insert(root, 1, Bytes(4096, 'z'));
    } catch (std::runtime_error&) {
        threw = true;
    }
    CHECK(threw);
}

int main() {
    struct Case { const char* name; void (*fn)(); };
    int only_deep = std::getenv("ONLY_DEEP") ? 1 : 0;
    Case cases[] = {
        {"crc_vector", test_crc_vector},
        {"insert_search_basic", test_insert_search_basic},
        {"duplicate_throws", test_duplicate_throws},
        {"shuffled_sorted_range", test_shuffled_sorted_range},
        {"upsert_and_delete", test_upsert_and_delete},
        {"deep_tree_mass_deletes", test_deep_tree_with_mass_deletes},
        {"oracle_random_ops", test_oracle_random_ops},
        {"wal_recover_and_torn_tail", test_wal_recover_and_torn_tail},
        {"value_too_large_rejected", test_value_too_large_rejected},
    };
    for (auto& c : cases) {
        std::printf("[RUN ] %s\n", c.name);
        std::fflush(stdout);
        c.fn();
        std::printf("[PASS] %s\n", c.name);
        std::fflush(stdout);
    }
    if (failures == 0) {
        std::printf("ALL NATIVE TESTS PASSED\n");
        return 0;
    }
    std::printf("%d FAILURE(S)\n", failures);
    return 1;
}


