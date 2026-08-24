// leafdb-core : storage hot path (pager + B+ tree + WAL) as a C++17 library.
// On-disk formats are byte-compatible with the Python engine (big-endian
// node/frame encoding, zlib-polynomial CRC32), so either implementation can
// read files the other wrote.
#pragma once

#include <cstdint>
#include <cstring>
#include <fstream>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace ldb {

constexpr uint32_t NO_PAGE = 0xFFFFFFFFu;

using Bytes = std::vector<uint8_t>;

// ---- big-endian helpers (match Python struct '>...') ------------------------

inline void put_u32(Bytes& b, uint32_t v) {
    b.push_back(uint8_t(v >> 24));
    b.push_back(uint8_t(v >> 16));
    b.push_back(uint8_t(v >> 8));
    b.push_back(uint8_t(v));
}

inline void put_i64(Bytes& b, int64_t v) {
    uint64_t u = uint64_t(v);
    for (int s = 56; s >= 0; s -= 8) b.push_back(uint8_t(u >> s));
}

inline uint32_t get_u32(const uint8_t* p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) |
           (uint32_t(p[2]) << 8) | uint32_t(p[3]);
}

inline int64_t get_i64(const uint8_t* p) {
    uint64_t u = 0;
    for (int i = 0; i < 8; ++i) u = (u << 8) | p[i];
    return int64_t(u);
}

// ---- CRC32 (zlib polynomial, reflected) --------------------------------------

uint32_t crc32(const uint8_t* data, size_t len);

// ---- pager -------------------------------------------------------------------

class Pager {
public:
    explicit Pager(std::string path, uint32_t page_size = 4096);
    ~Pager();
    Pager(const Pager&) = delete;
    Pager& operator=(const Pager&) = delete;

    uint32_t page_size() const { return ps_; }
    uint32_t num_pages() const { return num_pages_; }

    uint32_t allocate();
    void get(uint32_t n, Bytes& out) const;
    void write(uint32_t n, const Bytes& data);
    void flush();

private:
    void raw_read(uint32_t n, Bytes& out) const;
    void raw_write(uint32_t n, const Bytes& data);

    std::string path_;
    uint32_t ps_;
    uint32_t num_pages_;
    mutable std::fstream f_;
    mutable std::ifstream rf_;
};

// ---- B+ tree -----------------------------------------------------------------

class DuplicateKeyError : public std::runtime_error {
public:
    explicit DuplicateKeyError(int64_t key)
        : std::runtime_error("duplicate key: " + std::to_string(key)), key_(key) {}
    int64_t key() const { return key_; }
private:
    int64_t key_;
};

struct LeafNode {
    std::vector<std::pair<int64_t, Bytes>> cells;
    uint32_t next_leaf = NO_PAGE;

    Bytes to_bytes() const;
    size_t serialized_size() const;
    static LeafNode from_bytes(const uint8_t* data, size_t len);
};

struct InternalNode {
    std::vector<int64_t> keys;
    std::vector<uint32_t> children;

    Bytes to_bytes() const;
    size_t serialized_size() const;
    static InternalNode from_bytes(const uint8_t* data, size_t len);
};

using Node = std::variant<LeafNode, InternalNode>;

struct BTreeStats {
    uint32_t depth = 0;
    uint32_t leaves = 0;
    uint32_t internal_nodes = 0;
    uint64_t keys = 0;
};

struct CheckResult {
    bool ok = true;
    uint64_t keys = 0;
    bool balanced = true;
};

class BTree {
public:
    explicit BTree(Pager& pager, uint32_t page_size = 4096);

    bool search(uint32_t root, int64_t key, Bytes& value_out) const;
    // returns the (possibly new) root page; throws DuplicateKeyError
    uint32_t insert(uint32_t root, int64_t key, const Bytes& value);
    // upsert semantics: insert or overwrite
    uint32_t upsert(uint32_t root, int64_t key, const Bytes& value);
    // returns new root; found=false when key absent
    uint32_t remove(uint32_t root, int64_t key, bool& found);

    // inclusive [lo, hi]; either bound may be null
    std::vector<std::pair<int64_t, Bytes>>
    range_scan(uint32_t root, const std::optional<int64_t>& lo,
               const std::optional<int64_t>& hi) const;

    BTreeStats stats(uint32_t root) const;
    CheckResult check(uint32_t root) const;

    // diagnostic: page numbers along the leaf chain from leftmost; stops at
    // cycle/max_hops instead of throwing (returns hops taken)
    std::vector<uint32_t> leaf_chain_pages(uint32_t root, uint32_t max_hops = 10000) const;

    // diagnostic: for each leaf page in chain order: page, cells, next
    struct ChainInfo { uint32_t page; size_t cells; int64_t first; uint32_t next; };
    std::vector<ChainInfo> debug_leaf_chain(uint32_t root, uint32_t max_hops = 60) const;

    Node load_public(uint32_t n) const { return load(n); }

    // allocates a page and stores a valid empty leaf (next = NO_PAGE);
    // always use this instead of raw allocate() for new roots
    uint32_t create_root();

    uint32_t page_size() const { return ps_; }

private:
    const Node load(uint32_t n) const;
    void store(uint32_t n, const Node& node);
    static uint32_t child_index(const InternalNode& in, int64_t key);
    bool underflow(const Node& n) const;
    bool can_spare(const Node& n) const;
    size_t min_bytes() const { return ps_ / 3; }

    std::optional<std::pair<int64_t, uint32_t>>
    insert_rec(uint32_t n, int64_t key, const Bytes& value);
    std::pair<int64_t, uint32_t> split_leaf(uint32_t n, LeafNode& node);
    std::pair<int64_t, uint32_t> split_internal(uint32_t n, InternalNode& node);

    bool remove_rec(uint32_t n, int64_t key);
    void rebalance(uint32_t pn, InternalNode& pnode, size_t i);
    void borrow_from_left(InternalNode& pnode, size_t i,
                          Node& left, Node& child);
    void borrow_from_right(InternalNode& pnode, size_t i,
                           Node& child, Node& right);
    static void merge_nodes(Node& low, const Node& high, int64_t sep);

    Pager& pager_;
    uint32_t ps_;
};

// ---- WAL -----------------------------------------------------------------------

constexpr uint32_t WAL_COMMIT_PAGE = 0xFFFFFFFFu;

class WAL {
public:
    explicit WAL(std::string path);
    ~WAL();

    // appends frames + commit record, fsyncs; returns end offset
    uint64_t append_batch(const std::map<uint32_t, Bytes>& pages,
                          uint32_t num_pages);
    void reset();
    void close();

private:
    std::string path_;
    std::ofstream f_;
};

// replays committed batches into db_path; discards torn tails.
// returns number of batches applied.
uint32_t wal_recover(const std::string& wal_path, const std::string& db_path);

}  // namespace ldb
