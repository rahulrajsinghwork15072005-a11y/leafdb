// leafdb-core CLI: bench / verify / dump against LeafDB page files.
#include "leafdb_core.hpp"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <random>
#include <string>

using namespace ldb;

static Bytes val64(uint64_t v) {
    Bytes b(8, 0);
    for (int i = 0; i < 8; ++i) b[i] = uint8_t(v >> (8 * i));
    return b;
}

static int cmd_bench(int argc, char** argv) {
    // usage: leafdb-core bench <path> <n_rows> [page_size]
    std::string path = argv[2];
    uint64_t n = (argc > 3) ? std::strtoull(argv[3], nullptr, 10) : 20000;
    uint32_t ps = (argc > 4) ? uint32_t(std::atoi(argv[4])) : 4096;

    std::remove((path + ".root").c_str());
    Pager pager(path, ps);
    BTree tree(pager, ps);
    uint32_t root = tree.create_root();

    auto t0 = std::chrono::steady_clock::now();
    for (uint64_t i = 1; i <= n; ++i)
        root = tree.insert(root, int64_t(i), val64(i * 2654435761ull));
    auto t1 = std::chrono::steady_clock::now();
    double ins_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

    t0 = std::chrono::steady_clock::now();
    uint64_t hits = 0;
    Bytes v;
    for (uint64_t i = 1; i <= n; ++i) {
        if (tree.search(root, int64_t(i), v)) ++hits;
    }
    auto t2 = std::chrono::steady_clock::now();
    double look_ms = std::chrono::duration<double, std::milli>(t2 - t0).count();

    pager.flush();
    std::ofstream rf(path + ".root");
    rf << root << "\n";

    std::printf("inserted %llu rows in %.0f ms (%.0f rows/s)\n",
                (unsigned long long)n, ins_ms, n / (ins_ms / 1000.0));
    std::printf("point lookups %llu in %.1f ms (%.2f us/op)%s\n",
                (unsigned long long)n, look_ms, look_ms * 1000.0 / n,
                hits == n ? "" : "  [MISS DETECTED]");
    BTreeStats st = tree.stats(root);
    std::printf("tree: depth=%u leaves=%u internal=%u keys=%llu\n",
                st.depth, st.leaves, st.internal_nodes,
                (unsigned long long)st.keys);
    return hits == n ? 0 : 1;
}

static int cmd_verify(int argc, char** argv) {
    // usage: leafdb-core verify --root N <path>
    if (argc < 4 || std::strcmp(argv[2], "--root") != 0) {
        std::printf("usage: leafdb-core verify --root N <path>\n");
        return 2;
    }
    uint32_t root = uint32_t(std::atoi(argv[3]));
    Pager pager(argv[4]);
    BTree tree(pager, pager.page_size());
    try {
        CheckResult cr = tree.check(root);
        BTreeStats st = tree.stats(root);
        std::printf("OK keys=%llu depth=%u\n",
                    (unsigned long long)cr.keys, st.depth);
        return 0;
    } catch (const std::exception& e) {
        std::printf("INVARIANT VIOLATION: %s\n", e.what());
        return 1;
    }
}

static int cmd_dump(int argc, char** argv) {
    // usage: leafdb-core dump --root N <path> [lo] [hi]
    if (argc < 4 || std::strcmp(argv[2], "--root") != 0) {
        std::printf("usage: leafdb-core dump --root N <path> [lo] [hi]\n");
        return 2;
    }
    uint32_t root = uint32_t(std::atoi(argv[3]));
    Pager pager(argv[4]);
    BTree tree(pager, pager.page_size());
    std::optional<int64_t> lo, hi;
    if (argc > 5) lo = std::atoll(argv[5]);
    if (argc > 6) hi = std::atoll(argv[6]);
    auto rows = tree.range_scan(root, lo, hi);
    for (auto& [k, v] : rows) std::printf("%lld\n", (long long)k);
    return 0;
}

int main(int argc, char** argv) {
    if (argc >= 2 && !std::strcmp(argv[1], "bench")) return cmd_bench(argc, argv);
    if (argc >= 2 && !std::strcmp(argv[1], "verify")) return cmd_verify(argc, argv);
    if (argc >= 2 && !std::strcmp(argv[1], "dump")) return cmd_dump(argc, argv);
    std::printf("leafdb-core native tool\n"
                "usage:\n"
                "  leafdb-core bench <path> <n_rows> [page_size]\n"
                "  leafdb-core verify --root N <path>\n"
                "  leafdb-core dump --root N <path> [lo] [hi]\n");
    return 2;
}



