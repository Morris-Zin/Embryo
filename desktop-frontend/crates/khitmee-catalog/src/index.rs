use crate::models::CatalogItem;
use memchr::memmem;
use rayon::prelude::*;
use std::collections::HashMap;
use std::ops::Range;

const ITEM_CODE: usize = 0;
const ITEM_NAME: usize = 1;
const STOCK_UOM: usize = 2;
const ITEM_GROUP: usize = 3;
const BRAND: usize = 4;
const BARCODE: usize = 5;
const BARCODE_TEXT: usize = 6;
const SHORT_ORIGINAL_FIELD_COUNT: usize = 3;
const SEARCH_FIELD_COUNT: usize = 5;
const SHORT_SEARCH_FIELD_COUNT: usize = 2;
const MISSING_INTERNED_FIELD: u32 = u32::MAX;

fn fold(value: &str) -> String {
    value.to_lowercase()
}

fn as_u32(value: usize) -> u32 {
    u32::try_from(value).expect("catalog row fits in the compact index")
}

fn as_u16(value: usize) -> u16 {
    u16::try_from(value).expect("ERP catalog field fits in the compact index")
}

fn hash_bytes(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

#[derive(Default)]
struct InternedText {
    original_text: String,
    search_text: String,
    original_ranges: Vec<(u32, u32)>,
    search_ranges: Vec<(u32, u32)>,
    lookup: HashMap<u64, Vec<u32>>,
}

impl InternedText {
    fn intern(&mut self, value: &str) -> u32 {
        let hash = hash_bytes(value.as_bytes());
        if let Some(candidates) = self.lookup.get(&hash) {
            if let Some(id) = candidates.iter().find(|id| self.original(**id) == value) {
                return *id;
            }
        }

        let id = as_u32(self.original_ranges.len());
        let original_start = as_u32(self.original_text.len());
        self.original_text.push_str(value);
        self.original_ranges
            .push((original_start, as_u32(value.len())));

        let folded = fold(value);
        let search_start = as_u32(self.search_text.len());
        self.search_text.push_str(&folded);
        self.search_ranges
            .push((search_start, as_u32(folded.len())));
        self.lookup.entry(hash).or_default().push(id);
        id
    }

    fn original(&self, id: u32) -> &str {
        let (start, len) = self.original_ranges[id as usize];
        &self.original_text[start as usize..(start + len) as usize]
    }

    fn search(&self, id: u32) -> &str {
        let (start, len) = self.search_ranges[id as usize];
        &self.search_text[start as usize..(start + len) as usize]
    }

    fn shrink_to_fit(&mut self) {
        self.original_text.shrink_to_fit();
        self.search_text.shrink_to_fit();
        self.original_ranges.shrink_to_fit();
        self.search_ranges.shrink_to_fit();
        self.lookup.shrink_to_fit();
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
enum BarcodeSource {
    Primary,
    TextToken,
}

#[derive(Clone, Copy, Debug)]
struct PreparedBarcodeEntry {
    hash: u64,
    source: BarcodeSource,
    start: u32,
    end: u32,
}

#[derive(Clone, Debug)]
struct PreparedItem {
    original_text: String,
    original_lengths: [u16; SHORT_ORIGINAL_FIELD_COUNT],
    original_barcode_text_len: u32,
    present_fields: u8,
    search_text: String,
    search_lengths: [u16; SHORT_SEARCH_FIELD_COUNT],
    search_barcode_text_len: u32,
    stock_uom: Option<String>,
    item_group: Option<String>,
    brand: Option<String>,
    barcode_entries: Vec<PreparedBarcodeEntry>,
    rate: f64,
    actual_qty: f64,
}

impl PreparedItem {
    fn new(row: CatalogItem) -> Self {
        let CatalogItem {
            item_code,
            item_name,
            stock_uom,
            item_group,
            brand,
            barcode,
            barcode_text,
            rate,
            actual_qty,
            modified: _,
            deleted: _,
        } = row;
        let original_fields = [
            (ITEM_CODE, item_code.as_str(), true),
            (
                ITEM_NAME,
                item_name.as_deref().unwrap_or(""),
                item_name.is_some(),
            ),
            (BARCODE, barcode.as_deref().unwrap_or(""), barcode.is_some()),
            (
                BARCODE_TEXT,
                barcode_text.as_deref().unwrap_or(""),
                barcode_text.is_some(),
            ),
        ];
        let mut present_fields = 0u8;
        if stock_uom.is_some() {
            present_fields |= 1 << STOCK_UOM;
        }
        if item_group.is_some() {
            present_fields |= 1 << ITEM_GROUP;
        }
        if brand.is_some() {
            present_fields |= 1 << BRAND;
        }
        let mut original_lengths = [0u16; SHORT_ORIGINAL_FIELD_COUNT];
        let mut original_barcode_text_len = 0u32;
        let original_len: usize = original_fields
            .iter()
            .map(|(_, field, _)| field.len())
            .sum();
        let mut original_text = String::with_capacity(original_len);
        for (index, (field_bit, field, present)) in original_fields.iter().enumerate() {
            if *present {
                present_fields |= 1 << field_bit;
            }
            if index < SHORT_ORIGINAL_FIELD_COUNT {
                original_lengths[index] = as_u16(field.len());
            } else {
                original_barcode_text_len = as_u32(field.len());
            }
            original_text.push_str(field);
        }

        let folded_fields = [
            fold(&item_code),
            fold(item_name.as_deref().unwrap_or("")),
            fold(barcode_text.as_deref().unwrap_or("")),
        ];
        let search_len = folded_fields.iter().map(|field| field.len()).sum::<usize>();
        let mut search_text = String::with_capacity(search_len);
        let mut search_lengths = [0u16; SHORT_SEARCH_FIELD_COUNT];
        let mut search_barcode_text_len = 0u32;
        for (index, field) in folded_fields.iter().enumerate() {
            if index < SHORT_SEARCH_FIELD_COUNT {
                search_lengths[index] = as_u16(field.len());
            } else {
                search_barcode_text_len = as_u32(field.len());
            }
            search_text.push_str(field);
        }

        let mut barcode_entries = Vec::new();
        let mut primary_folded = None;
        if let Some(barcode) = barcode.as_deref() {
            let folded = fold(barcode);
            if !folded.is_empty() {
                barcode_entries.push(PreparedBarcodeEntry {
                    hash: hash_bytes(folded.as_bytes()),
                    source: BarcodeSource::Primary,
                    start: 0,
                    end: 0,
                });
                primary_folded = Some(folded);
            }
        }
        let folded_barcode_text = &folded_fields[2];
        let text_base = folded_barcode_text.as_ptr() as usize;
        for token in folded_barcode_text.split_whitespace() {
            if token.is_empty() || primary_folded.as_deref() == Some(token) {
                continue;
            }
            let start = token.as_ptr() as usize - text_base;
            barcode_entries.push(PreparedBarcodeEntry {
                hash: hash_bytes(token.as_bytes()),
                source: BarcodeSource::TextToken,
                start: as_u32(start),
                end: as_u32(start + token.len()),
            });
        }

        Self {
            original_text,
            original_lengths,
            original_barcode_text_len,
            present_fields,
            search_text,
            search_lengths,
            search_barcode_text_len,
            stock_uom,
            item_group,
            brand,
            barcode_entries,
            rate,
            actual_qty,
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct IndexedItem {
    original_start: u32,
    original_lengths: [u16; SHORT_ORIGINAL_FIELD_COUNT],
    original_barcode_text_len: u32,
    present_fields: u8,
    search_start: u32,
    search_lengths: [u16; SHORT_SEARCH_FIELD_COUNT],
    search_barcode_text_len: u32,
    stock_uom_id: u32,
    item_group_id: u32,
    brand_id: u32,
    rate: f64,
    actual_qty: f64,
}

#[derive(Clone, Copy, Debug)]
struct BarcodeEntry {
    hash: u64,
    row: u32,
    source: BarcodeSource,
    start: u32,
    end: u32,
}

/// Derived searchable catalog representation. Returned rows preserve cashier-facing fields but
/// intentionally omit `modified`; that sync metadata remains in encrypted storage.
#[derive(Default)]
pub struct CatalogIndex {
    items: Vec<IndexedItem>,
    original_text: String,
    search_text: String,
    interned_fields: InternedText,
    exact_barcode: Vec<BarcodeEntry>,
}

#[derive(Default)]
pub(crate) struct CatalogIndexBuilder {
    index: CatalogIndex,
    pending_row: Option<CatalogItem>,
    prepare_buffer: Vec<CatalogItem>,
}

impl CatalogIndexBuilder {
    const PREPARE_CHUNK_ROWS: usize = 2048;

    pub(crate) fn append_sorted_rows(&mut self, rows: Vec<CatalogItem>) {
        for row in rows {
            if self
                .pending_row
                .as_ref()
                .is_some_and(|pending| pending.item_code == row.item_code)
            {
                self.pending_row = Some(row);
                continue;
            }
            if let Some(previous) = self.pending_row.replace(row) {
                self.prepare_buffer.push(previous);
                if self.prepare_buffer.len() >= Self::PREPARE_CHUNK_ROWS {
                    self.flush_prepare_buffer();
                }
            }
        }
    }

    fn flush_prepare_buffer(&mut self) {
        if self.prepare_buffer.is_empty() {
            return;
        }
        let rows = std::mem::take(&mut self.prepare_buffer);
        let prepared: Vec<PreparedItem> = rows.into_par_iter().map(PreparedItem::new).collect();
        self.merge_prepared(prepared);
    }

    fn merge_prepared(&mut self, prepared: Vec<PreparedItem>) {
        for item in prepared {
            let row = self.index.items.len();
            let original_start = as_u32(self.index.original_text.len());
            let search_start = as_u32(self.index.search_text.len());
            self.index.original_text.push_str(&item.original_text);
            self.index.search_text.push_str(&item.search_text);
            for entry in item.barcode_entries {
                self.index.exact_barcode.push(BarcodeEntry {
                    hash: entry.hash,
                    row: as_u32(row),
                    source: entry.source,
                    start: entry.start,
                    end: entry.end,
                });
            }
            let stock_uom_id = item
                .stock_uom
                .as_deref()
                .map(|value| self.index.interned_fields.intern(value))
                .unwrap_or(MISSING_INTERNED_FIELD);
            let item_group_id = item
                .item_group
                .as_deref()
                .map(|value| self.index.interned_fields.intern(value))
                .unwrap_or(MISSING_INTERNED_FIELD);
            let brand_id = item
                .brand
                .as_deref()
                .map(|value| self.index.interned_fields.intern(value))
                .unwrap_or(MISSING_INTERNED_FIELD);
            self.index.items.push(IndexedItem {
                original_start,
                original_lengths: item.original_lengths,
                original_barcode_text_len: item.original_barcode_text_len,
                present_fields: item.present_fields,
                search_start,
                search_lengths: item.search_lengths,
                search_barcode_text_len: item.search_barcode_text_len,
                stock_uom_id,
                item_group_id,
                brand_id,
                rate: item.rate,
                actual_qty: item.actual_qty,
            });
        }
    }

    pub(crate) fn finish(mut self) -> CatalogIndex {
        if let Some(row) = self.pending_row.take() {
            self.prepare_buffer.push(row);
        }
        self.flush_prepare_buffer();
        self.index.exact_barcode.sort_by(|left, right| {
            left.hash
                .cmp(&right.hash)
                .then_with(|| left.row.cmp(&right.row))
                .then_with(|| left.source.cmp(&right.source))
                .then_with(|| left.start.cmp(&right.start))
                .then_with(|| left.end.cmp(&right.end))
        });
        self.index.interned_fields.lookup = HashMap::new();
        self.index.exact_barcode.shrink_to_fit();
        self.index.items.shrink_to_fit();
        self.index.original_text.shrink_to_fit();
        self.index.search_text.shrink_to_fit();
        self.index.interned_fields.shrink_to_fit();
        self.index
    }
}

impl CatalogIndex {
    pub fn rebuild(mut rows: Vec<CatalogItem>) -> Self {
        rows.retain(|row| !row.deleted && !row.item_code.is_empty());
        rows.par_sort_by(|left, right| left.item_code.cmp(&right.item_code));
        rows.dedup_by(|left, right| {
            if left.item_code == right.item_code {
                *left = right.clone();
                true
            } else {
                false
            }
        });

        let mut builder = CatalogIndexBuilder::default();
        builder.append_sorted_rows(rows);
        builder.finish()
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    fn inline_original_index(field: usize) -> usize {
        match field {
            ITEM_CODE => 0,
            ITEM_NAME => 1,
            BARCODE => 2,
            BARCODE_TEXT => 3,
            _ => unreachable!("interned catalog field has no inline range"),
        }
    }

    fn original_length(item: &IndexedItem, field: usize) -> usize {
        if field == SHORT_ORIGINAL_FIELD_COUNT {
            item.original_barcode_text_len as usize
        } else {
            item.original_lengths[field] as usize
        }
    }

    fn original_range(item: &IndexedItem, field: usize) -> Range<usize> {
        let field = Self::inline_original_index(field);
        let mut start = item.original_start as usize;
        for index in 0..field {
            start += Self::original_length(item, index);
        }
        start..start + Self::original_length(item, field)
    }

    fn original_field(&self, item: &IndexedItem, field: usize) -> Option<&str> {
        if item.present_fields & (1 << field) == 0 {
            return None;
        }
        match field {
            STOCK_UOM => Some(self.interned_fields.original(item.stock_uom_id)),
            ITEM_GROUP => Some(self.interned_fields.original(item.item_group_id)),
            BRAND => Some(self.interned_fields.original(item.brand_id)),
            _ => Some(&self.original_text[Self::original_range(item, field)]),
        }
    }

    fn item_code(&self, item: &IndexedItem) -> &str {
        &self.original_text[Self::original_range(item, ITEM_CODE)]
    }

    fn inline_search_index(field: usize) -> usize {
        match field {
            0 => 0,
            1 => 1,
            4 => 2,
            _ => unreachable!("interned search field has no inline range"),
        }
    }

    fn search_length(item: &IndexedItem, field: usize) -> usize {
        if field == SHORT_SEARCH_FIELD_COUNT {
            item.search_barcode_text_len as usize
        } else {
            item.search_lengths[field] as usize
        }
    }

    fn search_range(item: &IndexedItem, field: usize) -> Range<usize> {
        let field = Self::inline_search_index(field);
        let mut start = item.search_start as usize;
        for index in 0..field {
            start += Self::search_length(item, index);
        }
        start..start + Self::search_length(item, field)
    }

    fn search_field(&self, item: &IndexedItem, field: usize) -> &str {
        match field {
            2 if item.item_group_id != MISSING_INTERNED_FIELD => {
                self.interned_fields.search(item.item_group_id)
            }
            3 if item.brand_id != MISSING_INTERNED_FIELD => {
                self.interned_fields.search(item.brand_id)
            }
            2 | 3 => "",
            _ => &self.search_text[Self::search_range(item, field)],
        }
    }

    fn catalog_item(&self, item: &IndexedItem) -> CatalogItem {
        CatalogItem {
            item_code: self.item_code(item).to_owned(),
            item_name: self.original_field(item, ITEM_NAME).map(str::to_owned),
            stock_uom: self.original_field(item, STOCK_UOM).map(str::to_owned),
            item_group: self.original_field(item, ITEM_GROUP).map(str::to_owned),
            brand: self.original_field(item, BRAND).map(str::to_owned),
            barcode: self.original_field(item, BARCODE).map(str::to_owned),
            barcode_text: self.original_field(item, BARCODE_TEXT).map(str::to_owned),
            rate: item.rate,
            actual_qty: item.actual_qty,
            // `modified` is sync metadata retained in encrypted storage; the search index does not
            // need to duplicate it for any cashier-facing lookup or rendering path.
            modified: None,
            deleted: false,
        }
    }

    pub fn get_by_item_code(&self, item_code: &str) -> Option<CatalogItem> {
        self.items
            .binary_search_by(|item| self.item_code(item).cmp(item_code))
            .ok()
            .map(|position| self.catalog_item(&self.items[position]))
    }

    fn barcode_entry_matches(&self, entry: &BarcodeEntry, folded_term: &str) -> bool {
        let item = &self.items[entry.row as usize];
        match entry.source {
            BarcodeSource::Primary => self
                .original_field(item, BARCODE)
                .is_some_and(|barcode| fold(barcode) == folded_term),
            BarcodeSource::TextToken => {
                let barcode_text = self.search_field(item, 4);
                let start = entry.start as usize;
                let end = entry.end as usize;
                barcode_text.get(start..end) == Some(folded_term)
            }
        }
    }

    fn exact_barcode_position(&self, folded_term: &str) -> Option<usize> {
        let hash = hash_bytes(folded_term.as_bytes());
        let start = self
            .exact_barcode
            .partition_point(|entry| entry.hash < hash);
        self.exact_barcode[start..]
            .iter()
            .take_while(|entry| entry.hash == hash)
            .find(|entry| self.barcode_entry_matches(entry, folded_term))
            .map(|entry| entry.row as usize)
    }

    pub fn search(&self, term: &str, limit: usize) -> Vec<CatalogItem> {
        let folded_term = fold(term.trim());
        if folded_term.is_empty() || limit == 0 {
            return Vec::new();
        }
        let limit = limit.min(25);
        if let Some(position) = self.exact_barcode_position(&folded_term) {
            // A complete barcode identifies one sellable unit. Returning immediately avoids
            // scanning the whole catalog on the dominant cashier path.
            return vec![self.catalog_item(&self.items[position])];
        }

        let mut prefix = Vec::with_capacity(limit);
        let mut substring = Vec::with_capacity(limit);
        for (position, item) in self.items.iter().enumerate() {
            if prefix.len() == limit {
                break;
            }
            let is_prefix = (0..4).any(|field| {
                self.search_field(item, field)
                    .starts_with(folded_term.as_str())
            });
            if is_prefix {
                if prefix.len() < limit {
                    prefix.push(position);
                }
            } else if substring.len() < limit
                && (0..SEARCH_FIELD_COUNT).any(|field| {
                    memmem::find(
                        self.search_field(item, field).as_bytes(),
                        folded_term.as_bytes(),
                    )
                    .is_some()
                })
            {
                substring.push(position);
            }
        }

        prefix
            .into_iter()
            .chain(substring)
            .take(limit)
            .map(|position| self.catalog_item(&self.items[position]))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(code: &str, name: &str, barcode_text: &str) -> CatalogItem {
        CatalogItem {
            item_code: code.to_string(),
            item_name: Some(name.to_string()),
            stock_uom: Some("Nos".to_string()),
            item_group: Some("Tools".to_string()),
            brand: Some("House".to_string()),
            barcode: barcode_text.split_whitespace().next().map(str::to_string),
            barcode_text: Some(barcode_text.to_string()),
            rate: 1.0,
            actual_qty: 2.0,
            modified: None,
            deleted: false,
        }
    }

    #[test]
    fn exact_barcode_wins_then_prefix_then_substring() {
        let index = CatalogIndex::rebuild(vec![
            row("A-PREFIX", "Red Hammer", "111"),
            row("B-SUBSTRING", "Large Red Nail", "222"),
            row("C-EXACT", "Other", "red"),
        ]);
        let hits = index.search("red", 10);
        let codes: Vec<&str> = hits.iter().map(|row| row.item_code.as_str()).collect();
        assert_eq!(codes, vec!["C-EXACT"]);

        let hits = index.search("ham", 10);
        let codes: Vec<&str> = hits.iter().map(|row| row.item_code.as_str()).collect();
        assert_eq!(codes, vec!["A-PREFIX"]);
    }

    #[test]
    fn substring_matches_do_not_hide_later_prefix_matches() {
        let mut rows = Vec::new();
        for index in 0..50 {
            rows.push(row(
                &format!("S-{index:02}"),
                &format!("Large red substring {index}"),
                &format!("9{index:03}"),
            ));
        }
        rows.push(row("RED-PREFIX", "Red final", "8888"));
        let index = CatalogIndex::rebuild(rows);
        let hits = index.search("red", 10);
        assert_eq!(
            hits.first().map(|row| row.item_code.as_str()),
            Some("RED-PREFIX")
        );
    }

    #[test]
    fn deleted_rows_are_removed() {
        let mut deleted = row("A", "A", "1");
        deleted.deleted = true;
        let index = CatalogIndex::rebuild(vec![deleted]);
        assert_eq!(index.len(), 0);
    }

    #[test]
    fn burmese_names_are_searchable_without_lossy_ascii_folding() {
        let index = CatalogIndex::rebuild(vec![row("MM-1", "ဆန်၀ါး ၅ ကီလို", "111")]);
        let hits = index.search("ဆန်", 10);
        assert_eq!(hits.first().map(|row| row.item_code.as_str()), Some("MM-1"));
    }

    #[test]
    fn exact_item_code_lookup_uses_canonical_catalog_row() {
        let index = CatalogIndex::rebuild(vec![
            row("B-CODE", "Second", "222"),
            row("A-CODE", "First", "111"),
        ]);
        assert_eq!(
            index
                .get_by_item_code("A-CODE")
                .and_then(|row| row.item_name),
            Some("First".to_string())
        );
        assert!(index.get_by_item_code("a-code").is_none());
    }

    #[test]
    fn interned_dimension_fields_roundtrip_and_search_independently() {
        let mut first = row("A-TOOLS", "Hammer", "111");
        first.stock_uom = Some("Box".to_string());
        first.item_group = Some("Tools".to_string());
        first.brand = Some("House".to_string());
        let mut second = row("B-GROCERY", "Rice", "222");
        second.stock_uom = Some("Nos".to_string());
        second.item_group = Some("Grocery".to_string());
        second.brand = Some("Khitmee".to_string());

        let index = CatalogIndex::rebuild(vec![first, second]);
        let hits = index.search("groc", 10);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].item_code, "B-GROCERY");
        assert_eq!(hits[0].stock_uom.as_deref(), Some("Nos"));
        assert_eq!(hits[0].item_group.as_deref(), Some("Grocery"));
        assert_eq!(hits[0].brand.as_deref(), Some("Khitmee"));

        let first = index.get_by_item_code("A-TOOLS").expect("first row");
        assert_eq!(first.stock_uom.as_deref(), Some("Box"));
        assert_eq!(first.item_group.as_deref(), Some("Tools"));
        assert_eq!(first.brand.as_deref(), Some("House"));
    }

    #[test]
    fn long_barcode_text_indexes_without_truncation() {
        let token = "98765432101234";
        let barcode_text = format!("{} {}", "x".repeat(66_000), token);
        let mut long = row("LONG-BARCODE", "Long barcode list", "111");
        long.barcode = None;
        long.barcode_text = Some(barcode_text.clone());

        let decoy = row("DECOY", token, "222");
        let index = CatalogIndex::rebuild(vec![long, decoy]);
        assert!(index.exact_barcode_position(token).is_some());
        let hits = index.search(token, 10);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].item_code, "LONG-BARCODE");
        assert_eq!(hits[0].barcode_text.as_deref(), Some(barcode_text.as_str()));
    }

    #[test]
    fn compact_row_metadata_stays_small() {
        assert!(std::mem::size_of::<IndexedItem>() <= 56);
    }

    fn reference_search(rows: &[CatalogItem], term: &str, limit: usize) -> Vec<String> {
        let folded_term = term.trim().to_lowercase();
        if folded_term.is_empty() || limit == 0 {
            return Vec::new();
        }
        let limit = limit.min(25);
        let mut ordered: Vec<CatalogItem> = rows
            .iter()
            .filter(|row| !row.deleted && !row.item_code.is_empty())
            .cloned()
            .collect();
        ordered.sort_by(|left, right| left.item_code.cmp(&right.item_code));
        for row in &ordered {
            let barcodes = row
                .barcode
                .iter()
                .map(|value| value.to_lowercase())
                .chain(
                    row.barcode_text
                        .as_deref()
                        .unwrap_or("")
                        .split_whitespace()
                        .map(str::to_lowercase),
                )
                .collect::<Vec<_>>();
            if barcodes.iter().any(|barcode| barcode == &folded_term) {
                return vec![row.item_code.clone()];
            }
        }
        let mut prefix = Vec::new();
        let mut substring = Vec::new();
        for row in ordered {
            let fields = [
                row.item_code.to_lowercase(),
                row.item_name.clone().unwrap_or_default().to_lowercase(),
                row.item_group.clone().unwrap_or_default().to_lowercase(),
                row.brand.clone().unwrap_or_default().to_lowercase(),
            ];
            if fields.iter().any(|field| field.starts_with(&folded_term)) {
                prefix.push(row.item_code.clone());
                continue;
            }
            let search_text = fields.join("\u{1f}")
                + "\u{1f}"
                + &row.barcode_text.clone().unwrap_or_default().to_lowercase();
            if search_text.contains(&folded_term) {
                substring.push(row.item_code.clone());
            }
        }
        prefix.into_iter().chain(substring).take(limit).collect()
    }

    proptest::proptest! {
        #[test]
        fn exact_ascii_barcode_is_a_single_fast_result(barcode in "[0-9]{8,14}") {
            let mut exact = row("EXACT", "Barcode target", &barcode);
            exact.barcode = Some(barcode.clone());
            let index = CatalogIndex::rebuild(vec![
                row("PREFIX", &format!("{barcode} prefix"), "99999999999999"),
                exact,
            ]);
            let hits = index.search(&barcode, 25);
            proptest::prop_assert_eq!(hits.len(), 1);
            proptest::prop_assert_eq!(hits[0].item_code.as_str(), "EXACT");
        }

        #[test]
        fn indexed_search_matches_naive_reference_oracle(
            term in proptest::prop_oneof![
                "[a-z0-9 ]{0,12}",
                proptest::sample::select(vec!["red".to_string(), "ham".to_string(), "8888".to_string(), "ဆန်".to_string(), "retail".to_string()])
            ],
            limit in 0usize..30
        ) {
            let rows = vec![
                row("A-PREFIX", "Red Hammer", "111"),
                row("B-SUBSTRING", "Large Red Nail", "222 8888"),
                row("C-EXACT", "Other", "red"),
                row("MM-1", "ဆန်၀ါး ၅ ကီလို", "333"),
                row("D-RETAIL", "Grocery retail pack", "444"),
            ];
            let index = CatalogIndex::rebuild(rows.clone());
            let actual: Vec<String> = index.search(&term, limit).into_iter().map(|row| row.item_code).collect();
            proptest::prop_assert_eq!(actual, reference_search(&rows, &term, limit));
        }
    }
}
