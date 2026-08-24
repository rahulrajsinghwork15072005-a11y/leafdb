#include "leafdb_core.hpp"

#include <algorithm>
#include <cstdio>
#include <chrono>

namespace ldb {

// ---- CRC32 (reflected, polynomial 0xEDB88320 — identical to zlib) -----------

namespace {
const uint32_t* crc_table() {
    static uint32_t table[256];
    static bool init = [] {
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int k = 0; k < 8; ++k)
                c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            table[i] = c;
        }
        return true;
    }();
    (void)init;
    return table;
}
}  // namespace

uint32_t crc32(const uint8_t* data, size_t len) {
    const uint32_t* t = crc_table();
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; ++i)
        c = t[(c ^ data[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

// ---- node serialization (byte-compatible with Python engine) -----------------

constexpr uint8_t KIND_LEAF = 0x00;
constexpr uint8_t KIND_INTERNAL = 0x01;

Bytes LeafNode::to_bytes() const {
    Bytes b;
    b.reserve(9 + cells.size() * 16);
    b.push_back(KIND_LEAF);
    put_u32(b, next_leaf);
    put_u32(b, uint32_t(cells.size()));
    for (const auto& [k, v] : cells) {
        put_i64(b, k);
        put_u32(b, uint32_t(v.size()));
        b.insert(b.end(), v.begin(), v.end());
    }
    return b;
}

size_t LeafNode::serialized_size() const { return to_bytes().size(); }

LeafNode LeafNode::from_bytes(const uint8_t* d, size_t len) {
    if (len < 9 || d[0] != KIND_LEAF) throw std::runtime_error("bad leaf page");
    LeafNode n;
    n.next_leaf = get_u32(d + 1);
    uint32_t count = get_u32(d + 5);
    size_t off = 9;
    for (uint32_t i = 0; i < count; ++i) {
        if (off + 12 > len) throw std::runtime_error("truncated leaf");
        int64_t k = get_i64(d + off);
        uint32_t vlen = get_u32(d + off + 8);
        off += 12;
        if (off + vlen > len) throw std::runtime_error("truncated leaf value");
        n.cells.emplace_back(k, Bytes(d + off, d + off + vlen));
        off += vlen;
    }
    return n;
}

Bytes InternalNode::to_bytes() const {
    Bytes b;
    b.reserve(9 + keys.size() * 12);
    b.push_back(KIND_INTERNAL);
    put_u32(b, uint32_t(keys.size()));
    put_u32(b, uint32_t(children.size()));
    for (int64_t k : keys) put_i64(b, k);
    for (uint32_t c : children) put_u32(b, c);
    return b;
}

size_t InternalNode::serialized_size() const { return to_bytes().size(); }

InternalNode InternalNode::from_bytes(const uint8_t* d, size_t len) {
    if (len < 9 || d[0] != KIND_INTERNAL) throw std::runtime_error("bad internal page");
    InternalNode n;
    uint32_t nk = get_u32(d + 1);
    uint32_t nc = get_u32(d + 5);
    size_t off = 9;
    for (uint32_t i = 0; i < nk; ++i) {
        if (off + 8 > len) throw std::runtime_error("truncated internal");
        n.keys.push_back(get_i64(d + off));
        off += 8;
    }
    for (uint32_t i = 0; i < nc; ++i) {
        if (off + 4 > len) throw std::runtime_error("truncated internal");
        n.children.push_back(get_u32(d + off));
        off += 4;
    }
    return n;
}

// ---- pager ---------------------------------------------------------------------

static bool file_exists(const std::string& p) {
    std::ifstream f(p.c_str(), std::ios::binary);
    return f.good();
}

Pager::Pager(std::string path, uint32_t page_size)
    : path_(std::move(path)), ps_(page_size) {
    if (!file_exists(path_)) {
        std::ofstream create(path_.c_str(), std::ios::binary);
    }
    f_.open(path_, std::ios::binary | std::ios::in | std::ios::out);
    if (!f_) throw std::runtime_error("cannot open " + path_);
    f_.seekg(0, std::ios::end);
    auto size = f_.tellg();
    num_pages_ = uint32_t(std::max<int64_t>(1, int64_t(size) / ps_));
}

Pager::~Pager() { try { flush(); } catch (...) {} }

uint32_t Pager::allocate() {
    uint32_t n = num_pages_++;
    Bytes zeros(ps_, 0);
    raw_write(n, zeros);
    return n;
}

void Pager::get(uint32_t n, Bytes& out) const {
    out.assign(ps_, 0);
    if (int64_t(n) * ps_ >= int64_t(num_pages_) * ps_) {
        // beyond EOF reads as zeros
        return;
    }
    raw_read(n, out);
}

void Pager::write(uint32_t n, const Bytes& data) {
    if (data.size() > ps_) throw std::runtime_error("page overflow");
    Bytes padded = data;
    padded.resize(ps_, 0);
    raw_write(n, padded);
    if (n >= num_pages_) num_pages_ = n + 1;
}

void Pager::raw_read(uint32_t n, Bytes& out) const {
    if (!rf_.is_open()) rf_.open(path_, std::ios::binary);
    rf_.clear();
    rf_.seekg(int64_t(n) * ps_);
    rf_.read(reinterpret_cast<char*>(out.data()), ps_);
    auto got = rf_.gcount();
    if (got < std::streamsize(ps_))
        std::fill(out.begin() + got, out.end(), 0);
}

void Pager::raw_write(uint32_t n, const Bytes& data) {
    f_.clear();
    f_.seekp(int64_t(n) * ps_);
    f_.write(reinterpret_cast<const char*>(data.data()), std::streamsize(data.size()));
    f_.flush();
    if (!f_) throw std::runtime_error("page write failed");
}

void Pager::flush() {
    f_.clear();
    f_.flush();
    // reads must observe our own writes even before the OS cache syncs
    rf_.close();
}

// ---- B+ tree -------------------------------------------------------------------

BTree::BTree(Pager& pager, uint32_t page_size) : pager_(pager), ps_(page_size) {}

const Node BTree::load(uint32_t n) const {
    Bytes buf;
    pager_.get(n, buf);
    if (buf.empty() || buf[0] == KIND_LEAF) return LeafNode::from_bytes(buf.data(), buf.size());
    return Node(InternalNode::from_bytes(buf.data(), buf.size()));
}

void BTree::store(uint32_t n, const Node& node) {
    Bytes b = std::visit([](const auto& nd) { return nd.to_bytes(); }, node);
    if (b.size() > ps_)
        throw std::runtime_error("node overflow: " + std::to_string(b.size()));
    if (std::holds_alternative<LeafNode>(node)) {
        auto& l = std::get<LeafNode>(node);
        (void)l;
    }
    pager_.write(n, b);
}

bool BTree::search(uint32_t root, int64_t key, Bytes& value_out) const {
    uint32_t n = root;
    while (true) {
        Node node = load(n);
        if (std::holds_alternative<LeafNode>(node)) {
            const auto& leaf = std::get<LeafNode>(node);
            auto it = std::lower_bound(
                leaf.cells.begin(), leaf.cells.end(), key,
                [](const auto& c, int64_t k) { return c.first < k; });
            if (it != leaf.cells.end() && it->first == key) {
                value_out = it->second;
                return true;
            }
            return false;
        }
        const auto& in = std::get<InternalNode>(node);
        size_t i = size_t(std::upper_bound(in.keys.begin(), in.keys.end(), key) - in.keys.begin());
        n = in.children[i];
    }
}

uint32_t BTree::insert(uint32_t root, int64_t key, const Bytes& value) {
    if (value.size() > ps_ / 2)
        throw std::runtime_error("value exceeds MAX_VALUE_SIZE");
    auto split = insert_rec(root, key, value);
    if (!split) return root;
    InternalNode new_root;
    new_root.keys.push_back(split->first);
    new_root.children.push_back(root);
    new_root.children.push_back(split->second);
    uint32_t n = pager_.allocate();
    store(n, Node(new_root));
    return n;
}

std::optional<std::pair<int64_t, uint32_t>>
BTree::insert_rec(uint32_t n, int64_t key, const Bytes& value) {
    Node node = load(n);
    if (std::holds_alternative<LeafNode>(node)) {
        LeafNode& leaf = std::get<LeafNode>(node);
        auto it = std::lower_bound(
            leaf.cells.begin(), leaf.cells.end(), key,
            [](const auto& c, int64_t k) { return c.first < k; });
        if (it != leaf.cells.end() && it->first == key) throw DuplicateKeyError(key);
        leaf.cells.insert(it, {key, value});
        if (leaf.serialized_size() <= ps_) {
            store(n, Node(leaf));
            return std::nullopt;
        }
        return split_leaf(n, leaf);
    }
    InternalNode& in = std::get<InternalNode>(node);
    size_t i = size_t(std::upper_bound(in.keys.begin(), in.keys.end(), key) - in.keys.begin());
    auto split = insert_rec(in.children[i], key, value);
    if (!split) return std::nullopt;
    in.keys.insert(in.keys.begin() + long(i), split->first);
    in.children.insert(in.children.begin() + long(i) + 1, split->second);
    if (in.serialized_size() <= ps_) {
        store(n, Node(in));
        return std::nullopt;
    }
    return split_internal(n, in);
}

std::pair<int64_t, uint32_t> BTree::split_leaf(uint32_t n, LeafNode& node) {
    size_t mid = node.cells.size() / 2;
    if (mid == 0) throw std::runtime_error("row too large to fit in a page");
    uint32_t right_page = pager_.allocate();
    LeafNode right;
    right.cells.assign(node.cells.begin() + long(mid), node.cells.end());
    right.next_leaf = node.next_leaf;
    LeafNode left;
    left.cells.assign(node.cells.begin(), node.cells.begin() + long(mid));
    left.next_leaf = right_page;
    store(right_page, Node(right));
    store(n, Node(left));
    return {right.cells.front().first, right_page};
}

std::pair<int64_t, uint32_t> BTree::split_internal(uint32_t n, InternalNode& node) {
    size_t mid = node.keys.size() / 2;
    if (mid == 0) throw std::runtime_error("internal node too small to split");
    int64_t sep = node.keys[mid];
    uint32_t right_page = pager_.allocate();
    InternalNode right;
    right.keys.assign(node.keys.begin() + long(mid) + 1, node.keys.end());
    right.children.assign(node.children.begin() + long(mid) + 1, node.children.end());
    InternalNode left;
    left.keys.assign(node.keys.begin(), node.keys.begin() + long(mid));
    left.children.assign(node.children.begin(), node.children.begin() + long(mid) + 1);
    store(right_page, Node(right));
    store(n, Node(left));
    return {sep, right_page};
}

uint32_t BTree::upsert(uint32_t root, int64_t key, const Bytes& value) {
    try {
        return insert(root, key, value);
    } catch (DuplicateKeyError&) {
        uint32_t n = root;
        while (true) {
            Node node = load(n);
            if (std::holds_alternative<LeafNode>(node)) {
                LeafNode& leaf = std::get<LeafNode>(node);
                auto it = std::lower_bound(
                    leaf.cells.begin(), leaf.cells.end(), key,
                    [](const auto& c, int64_t k) { return c.first < k; });
                it->second = value;
                store(n, Node(leaf));
                return root;
            }
            InternalNode& in = std::get<InternalNode>(node);
            n = in.children[size_t(std::upper_bound(in.keys.begin(), in.keys.end(), key) - in.keys.begin())];
        }
    }
}

bool BTree::underflow(const Node& n) const {
    if (std::holds_alternative<LeafNode>(n)) {
        const auto& l = std::get<LeafNode>(n);
        return l.cells.empty() || l.serialized_size() < min_bytes();
    }
    const auto& in = std::get<InternalNode>(n);
    return in.children.size() < 2 || in.serialized_size() < min_bytes();
}

bool BTree::can_spare(const Node& n) const {
    return int64_t(std::visit([](const auto& nd) { return nd.serialized_size(); }, n)) -
               int64_t(min_bytes()) > 64;
}

uint32_t BTree::remove(uint32_t root, int64_t key, bool& found) {
    found = remove_rec(root, key);
    if (!found) return root;
    Node node = load(root);
    while (std::holds_alternative<InternalNode>(node) &&
           std::get<InternalNode>(node).keys.empty()) {
        root = std::get<InternalNode>(node).children[0];
        node = load(root);
    }
    return root;
}

bool BTree::remove_rec(uint32_t n, int64_t key) {
    Node node = load(n);
    if (std::holds_alternative<LeafNode>(node)) {
        LeafNode& leaf = std::get<LeafNode>(node);
        auto it = std::lower_bound(
            leaf.cells.begin(), leaf.cells.end(), key,
            [](const auto& c, int64_t k) { return c.first < k; });
        if (it != leaf.cells.end() && it->first == key) {
            leaf.cells.erase(it);
            store(n, Node(leaf));
            return true;
        }
        return false;
    }
    InternalNode& in = std::get<InternalNode>(node);
    size_t i = size_t(std::upper_bound(in.keys.begin(), in.keys.end(), key) - in.keys.begin());
    bool deleted = remove_rec(in.children[i], key);
    if (!deleted) return false;
    Node child = load(in.children[i]);
    if (underflow(child)) {
        rebalance(n, in, i);
        store(n, Node(in));
    }
    return true;
}

void BTree::rebalance(uint32_t pn, InternalNode& pnode, size_t i) {
    uint32_t child_page = pnode.children[i];
    Node child = load(child_page);

    if (i > 0) {
        Node left = load(pnode.children[i - 1]);
        if (can_spare(left)) {
            borrow_from_left(pnode, i, left, child);
            store(pnode.children[i - 1], left);
            store(child_page, child);
            store(pn, Node(pnode));
            return;
        }
    }
    if (i + 1 < pnode.children.size()) {
        Node right = load(pnode.children[i + 1]);
        if (can_spare(right)) {
            borrow_from_right(pnode, i, child, right);
            store(pnode.children[i + 1], right);
            store(child_page, child);
            store(pn, Node(pnode));
            return;
        }
    }

    if (i > 0) {
        Node left = load(pnode.children[i - 1]);
        merge_nodes(left, child, pnode.keys[i - 1]);
        store(pnode.children[i - 1], left);
        pnode.children.erase(pnode.children.begin() + long(i));
        pnode.keys.erase(pnode.keys.begin() + long(i) - 1);
    } else {
        Node right = load(pnode.children[i + 1]);
        merge_nodes(child, right, pnode.keys[i]);
        store(child_page, child);
        pnode.children.erase(pnode.children.begin() + long(i) + 1);
        pnode.keys.erase(pnode.keys.begin() + long(i));
    }
}

void BTree::borrow_from_left(InternalNode& pnode, size_t i,
                             Node& left, Node& child) {
    if (std::holds_alternative<LeafNode>(child)) {
        auto& l = std::get<LeafNode>(left);
        auto& c = std::get<LeafNode>(child);
        c.cells.insert(c.cells.begin(), l.cells.back());
        l.cells.pop_back();
        pnode.keys[i - 1] = c.cells.front().first;
    } else {
        auto& l = std::get<InternalNode>(left);
        auto& c = std::get<InternalNode>(child);
        c.keys.insert(c.keys.begin(), pnode.keys[i - 1]);
        pnode.keys[i - 1] = l.keys.back();
        l.keys.pop_back();
        c.children.insert(c.children.begin(), l.children.back());
        l.children.pop_back();
    }
}

void BTree::borrow_from_right(InternalNode& pnode, size_t i,
                              Node& child, Node& right) {
    if (std::holds_alternative<LeafNode>(child)) {
        auto& r = std::get<LeafNode>(right);
        auto& c = std::get<LeafNode>(child);
        c.cells.push_back(r.cells.front());
        r.cells.erase(r.cells.begin());
        pnode.keys[i] = r.cells.front().first;
    } else {
        auto& r = std::get<InternalNode>(right);
        auto& c = std::get<InternalNode>(child);
        c.keys.push_back(pnode.keys[i]);
        pnode.keys[i] = r.keys.front();
        r.keys.erase(r.keys.begin());
        c.children.push_back(r.children.front());
        r.children.erase(r.children.begin());
    }
}

void BTree::merge_nodes(Node& low, const Node& high, int64_t sep) {
    if (std::holds_alternative<LeafNode>(low)) {
        auto& l = std::get<LeafNode>(low);
        const auto& h = std::get<LeafNode>(high);
        l.cells.insert(l.cells.end(), h.cells.begin(), h.cells.end());
        l.next_leaf = h.next_leaf;
        return;
    }
    auto& l = std::get<InternalNode>(low);
    const auto& h = std::get<InternalNode>(high);
    l.keys.push_back(sep);
    l.keys.insert(l.keys.end(), h.keys.begin(), h.keys.end());
    l.children.insert(l.children.end(), h.children.begin(), h.children.end());
}

std::vector<std::pair<int64_t, Bytes>>
BTree::range_scan(uint32_t root, const std::optional<int64_t>& lo,
                  const std::optional<int64_t>& hi) const {
    std::vector<std::pair<int64_t, Bytes>> out;
    uint32_t n = root;
    while (true) {
        Node node = load(n);
        if (std::holds_alternative<LeafNode>(node)) break;
        const auto& in = std::get<InternalNode>(node);
        n = lo ? in.children[size_t(std::upper_bound(in.keys.begin(), in.keys.end(), *lo) - in.keys.begin())]
               : in.children[0];
    }
    while (true) {
        Node node = load(n);
        const auto& leaf = std::get<LeafNode>(node);
        for (const auto& [k, v] : leaf.cells) {
            if (lo && k < *lo) continue;
            if (hi && k > *hi) return out;
            out.emplace_back(k, v);
        }
        if (leaf.next_leaf == NO_PAGE || leaf.next_leaf == 0) break;
        n = leaf.next_leaf;
    }
    return out;
}

BTreeStats BTree::stats(uint32_t root) const {
    BTreeStats st;
    st.depth = 1;
    std::vector<uint32_t> level{root};
    while (!level.empty()) {
        std::vector<uint32_t> nxt;
        for (uint32_t pg : level) {
            Node nd = load(pg);
            if (std::holds_alternative<LeafNode>(nd)) {
                ++st.leaves;
                st.keys += std::get<LeafNode>(nd).cells.size();
            } else {
                ++st.internal_nodes;
                const auto& in = std::get<InternalNode>(nd);
                nxt.insert(nxt.end(), in.children.begin(), in.children.end());
            }
        }
        if (!nxt.empty()) ++st.depth;
        level = std::move(nxt);
    }
    return st;
}

uint32_t BTree::create_root() {
    uint32_t n = pager_.allocate();
    store(n, Node(LeafNode{}));
    return n;
}

std::vector<uint32_t> BTree::leaf_chain_pages(uint32_t root, uint32_t max_hops) const {
    std::vector<uint32_t> pages;
    std::map<uint32_t, bool> seen;
    uint32_t n = root;
    Node node = load(n);
    while (std::holds_alternative<InternalNode>(node)) {
        n = std::get<InternalNode>(node).children[0];
        node = load(n);
    }
    while (true) {
        if (!std::holds_alternative<LeafNode>(node))
            throw std::runtime_error("leaf chain hit non-leaf");
        if (seen.count(n)) throw std::runtime_error("cycle in leaf chain");
        seen[n] = true;
        pages.push_back(n);
        uint32_t nxt = std::get<LeafNode>(node).next_leaf;
        if (nxt == NO_PAGE || nxt == 0 || pages.size() >= max_hops) break;
        n = nxt;
        node = load(n);
    }
    return pages;
}

std::vector<BTree::ChainInfo> BTree::debug_leaf_chain(uint32_t root, uint32_t max_hops) const {
    std::vector<ChainInfo> out;
    uint32_t n = root;
    Node node = load(n);
    while (std::holds_alternative<InternalNode>(node)) {
        n = std::get<InternalNode>(node).children[0];
        node = load(n);
    }
    for (uint32_t hop = 0; hop < max_hops; ++hop) {
        if (!std::holds_alternative<LeafNode>(node)) {
            out.push_back({n, SIZE_MAX, -1, UINT32_MAX});
            break;
        }
        auto& leaf = std::get<LeafNode>(node);
        out.push_back({n, leaf.cells.size(),
                       leaf.cells.empty() ? -1 : leaf.cells.front().first,
                       leaf.next_leaf});
        if (leaf.next_leaf == NO_PAGE || leaf.next_leaf == 0) break;
        n = leaf.next_leaf;
        node = load(n);
    }
    return out;
}

CheckResult BTree::check(uint32_t root) const {
    CheckResult res;
    std::vector<int64_t> walked;
    uint32_t min_depth = UINT32_MAX, max_depth = 0;

    struct Item { uint32_t page; uint32_t depth; };
    // ordered walk via recursion
    // (recursive lambda through helper below)
    struct Walker {
        const BTree& self;
        std::vector<int64_t>& walked;
        uint32_t& min_depth;
        uint32_t& max_depth;

        std::pair<std::optional<int64_t>, std::optional<int64_t>>
        go(uint32_t pg, uint32_t depth, bool is_root) {
            Node nd = self.load(pg);
            if (std::holds_alternative<LeafNode>(nd)) {
                min_depth = std::min(min_depth, depth);
                max_depth = std::max(max_depth, depth);
                auto& leaf = std::get<LeafNode>(nd);
                for (auto& [k, v] : leaf.cells) walked.push_back(k);
                for (size_t j = 1; j < leaf.cells.size(); ++j)
                    if (!(leaf.cells[j - 1].first < leaf.cells[j].first))
                        throw std::runtime_error("leaf keys unsorted/dup at " + std::to_string(pg));
                if (!is_root && self.underflow(nd))
                    throw std::runtime_error("leaf underfull: " + std::to_string(pg));
                if (leaf.cells.empty())
                    return {std::nullopt, std::nullopt};
                return {leaf.cells.front().first, leaf.cells.back().first};
            }
            auto& in = std::get<InternalNode>(nd);
            if (in.children.size() != in.keys.size() + 1)
                throw std::runtime_error("children/separators mismatch at " + std::to_string(pg));
            for (size_t j = 1; j < in.keys.size(); ++j)
                if (!(in.keys[j - 1] < in.keys[j]))
                    throw std::runtime_error("separators unsorted at " + std::to_string(pg));
            if (!is_root && depth > 1 && self.underflow(nd))
                throw std::runtime_error("internal underfull: " + std::to_string(pg));

            std::optional<int64_t> lo, hi;
            std::vector<std::optional<int64_t>> mins;
            for (size_t j = 0; j < in.children.size(); ++j) {
                auto b = go(in.children[j], depth + 1, false);
                mins.push_back(b.first);
                if (!lo && b.first) lo = b.first;
                if (b.second) hi = b.second;
            }
            for (size_t j = 0; j < in.keys.size(); ++j) {
                if (!mins[j + 1] || *mins[j + 1] < in.keys[j])
                    throw std::runtime_error("unsafe separator at " + std::to_string(pg));
            }
            return {lo, hi};
        }
    } walker{*this, walked, min_depth, max_depth};

    walker.go(root, 1, true);

    // leaf chain must match tree order exactly (hop-capped: cycles cannot hang)
    std::vector<int64_t> linked;
    uint32_t guard = walked.size() + 2;
    uint32_t pg = root;
    Node nd = load(pg);
    while (std::holds_alternative<InternalNode>(nd)) {
        pg = std::get<InternalNode>(nd).children[0];
        nd = load(pg);
    }
    while (true) {
        if (!std::holds_alternative<LeafNode>(nd))
            throw std::runtime_error("leaf chain hit non-leaf");
        auto& leaf = std::get<LeafNode>(nd);
        for (auto& [k, _v] : leaf.cells) linked.push_back(k);
        if (leaf.next_leaf == NO_PAGE || leaf.next_leaf == 0) break;
        if (guard-- == 0)
            throw std::runtime_error("leaf chain exceeds tree size (cycle)");
        pg = leaf.next_leaf;
        nd = load(pg);
    }
    if (linked != walked) throw std::runtime_error("leaf chain disagrees with tree order");
    if (min_depth != max_depth) throw std::runtime_error("unbalanced tree");

    res.keys = walked.size();
    res.balanced = (min_depth == max_depth);
    return res;
}

// ---- WAL -------------------------------------------------------------------------

WAL::WAL(std::string path) : path_(std::move(path)) {
    bool fresh = !file_exists(path_) ||
                 std::ifstream(path_.c_str(), std::ios::binary)
                     .seekg(0, std::ios::end).tellg() < std::streamsize(10);
    f_.open(path_, std::ios::binary | (fresh ? std::ios::out : std::ios::in | std::ios::out));
    if (fresh) {
        f_.write("LEAFWAL01\n", 10);
        f_.flush();
    } else {
        f_.seekp(0, std::ios::end);
    }
}

WAL::~WAL() { try { close(); } catch (...) {} }

uint64_t WAL::append_batch(const std::map<uint32_t, Bytes>& pages,
                           uint32_t num_pages) {
    f_.seekp(0, std::ios::end);
    auto frame = [&](uint32_t page_no, const Bytes& payload) {
        Bytes fr;
        fr.reserve(16 + payload.size());
        fr.push_back('L'); fr.push_back('F'); fr.push_back('D'); fr.push_back('B');
        put_u32(fr, page_no);
        put_u32(fr, uint32_t(payload.size()));
        put_u32(fr, crc32(payload.data(), payload.size()));
        fr.insert(fr.end(), payload.begin(), payload.end());
        return fr;
    };
    Bytes blob;
    for (const auto& [pg, img] : pages) {
        Bytes fr = frame(pg, img);
        blob.insert(blob.end(), fr.begin(), fr.end());
    }
    Bytes commit_payload;
    put_u32(commit_payload, num_pages);
    Bytes commit_fr = frame(WAL_COMMIT_PAGE, commit_payload);
    blob.insert(blob.end(), commit_fr.begin(), commit_fr.end());
    f_.write(reinterpret_cast<const char*>(blob.data()), std::streamsize(blob.size()));
    f_.flush();
    return uint64_t(f_.tellp());
}

void WAL::reset() {
    f_.close();
    f_.open(path_, std::ios::binary | std::ios::out | std::ios::trunc);
    f_.write("LEAFWAL01\n", 10);
    f_.flush();
}

void WAL::close() {
    if (f_.is_open()) f_.close();
}

uint32_t wal_recover(const std::string& wal_path, const std::string& db_path) {
    if (!file_exists(db_path)) {
        std::ofstream c(db_path.c_str(), std::ios::binary);
    }
    if (!file_exists(wal_path)) return 0;
    std::ifstream wf(wal_path.c_str(), std::ios::binary);
    wf.seekg(0, std::ios::end);
    int64_t size = wf.tellg();
    if (size <= 10) return 0;
    wf.seekg(0);
    Bytes data(static_cast<size_t>(size));
    wf.read(reinterpret_cast<char*>(data.data()), size);

    size_t pos = 10;
    uint32_t applied = 0;
    std::map<uint32_t, Bytes> batch;
    std::fstream db(db_path.c_str(), std::ios::binary | std::ios::in | std::ios::out);
    while (pos + 16 <= data.size()) {
        const uint8_t* fh = data.data() + pos;
        if (std::memcmp(fh, "LFDB", 4) != 0) break;
        uint32_t page_no = get_u32(fh + 4);
        uint32_t length = get_u32(fh + 8);
        uint32_t crc = get_u32(fh + 12);
        size_t start = pos + 16;
        size_t stop = start + length;
        if (stop > data.size()) break;
        const uint8_t* payload = data.data() + start;
        if (crc32(payload, length) != crc) break;
        pos = stop;
        if (page_no == WAL_COMMIT_PAGE) {
            uint32_t num_pages = get_u32(payload);
            for (auto& [pg, img] : batch) {
                db.seekg(0, std::ios::end);
                int64_t db_end = db.tellg();
                int64_t want = int64_t(pg + 1) * 4096;
                if (db_end < want) {
                    db.seekp(want - 1);
                    db.write("\x00", 1);
                }
                db.seekp(int64_t(pg) * 4096);
                db.write(reinterpret_cast<const char*>(img.data()),
                         std::streamsize(img.size()));
            }
            db.flush();
            int64_t need = int64_t(num_pages) * 4096;
            db.seekg(0, std::ios::end);
            if (db.tellg() < need) {
                db.seekp(need - 1);
                db.write("\x00", 1);
                db.flush();
            }
            ++applied;
            batch.clear();
        } else {
            batch[page_no] = Bytes(payload, payload + length);
        }
    }
    return applied;
}

}  // namespace ldb







