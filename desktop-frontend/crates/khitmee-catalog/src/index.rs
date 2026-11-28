use crate::models::CatalogItem;
use memchr::memmem;
use rayon::prelude::*;
use std::ops::Range;

const ITEM_CODE: usize = 0;
const ITEM_NAME: usize = 1;
const STOCK_UOM: usize = 2;
const ITEM_GROUP: usize = 3;
const BRAND: usize = 4;
const BARCODE: usize = 5;
const BARCODE_TEXT: usize = 6;
const MODIFIED: usize = 7;
const ORIGINAL_FIELD_COUNT: usize = 8;
const SEARCH_FIELD_COUNT: usize = 5;
const SEARCH_SEPARATOR: char = '\u{1f}';

fn fold(value: &str) -> String {
    value.to_lowercase()
}

fn as_u32(value: usize) -> u32 {
    u32::try_from(value).expect("catalog field fits in the compact index")
}

fn hash_bytes(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
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
    original_lengths: [u32; ORIGINAL_FIELD_COUNT],
    present_fields: u8,
    search_text: String,
    search_lengths: [u32; SEARCH_FIELD_COUNT],
    barcode_entries: Vec<PreparedBarcodeEntry>,
    rate: f64,
    actual_qty: f64,
}

impl PreparedItem {
    fn new(row: CatalogItem) -> Self {
        let original_fields = [
            row.item_code.as_str(),
            row.item_name.as_deref().unwrap_or(""),
            row.stock_uom.as_deref().unwrap_or(""),
            row.item_group.as_deref().unwrap_or(""),
            row.brand.as_deref().unwrap_or(""),
            row.barcode.as_deref().unwrap_or(""),
            row.barcode_text.as_deref().unwrap_or(""),
            row.modified.as_deref().unwrap_or(""),
        ];
        let optional_fields = [
            false,
            row.item_name.is_some(),
            row.stock_uom.is_some(),
            row.item_group.is_some(),
            row.brand.is_some(),
            row.barcode.is_some(),
            row.barcode_text.is_some(),
            row.modified.is_some(),
        ];
        let mut present_fields = 0u8;
        let mut original_lengths = [0u32; ORIGINAL_FIELD_COUNT];
        let original_len: usize = original_fields.iter().map(|field| field.len()).sum();
        let mut original_text = String::with_capacity(original_len);
        for (index, field) in original_fields.iter().enumerate() {
            if optional_fields[index] {
                present_fields |= 1 << index;
            }
            original_lengths[index] = as_u32(field.len());
            original_text.push_str(field);
        }

        let folded_fields = [
            fold(&row.item_code),
            fold(row.item_name.as_deref().unwrap_or("")),
            fold(row.item_group.as_deref().unwrap_or("")),
            fold(row.brand.as_deref().unwrap_or("")),
            fold(row.barcode_text.as_deref().unwrap_or("")),
        ];
        let search_len = folded_fields.iter().map(|field| field.len()).sum::<usize>()
            + SEARCH_FIELD_COUNT.saturating_sub(1);
        let mut search_text = String::with_capacity(search_len);
        let mut search_lengths = [0u32; SEARCH_FIELD_COUNT];
        for (index, field) in folded_fields.iter().enumerate() {
            if index != 0 {
                search_text.push(SEARCH_SEPARATOR);
            }
            search_lengths[index] = as_u32(field.len());
            search_text.push_str(field);
        }

        let mut barcode_entries = Vec::new();
        if let Some(barcode) = row.barcode.as_deref() {
            let folded = fold(barcode);
            if !folded.is_empty() {
                barcode_entries.push(PreparedBarcodeEntry {
                    hash: hash_bytes(folded.as_bytes()),
                    source: BarcodeSource::Primary,
                    start: 0,
                    end: 0,
                });
            }
        }
        let folded_barcode_text = &folded_fields[4];
        let text_base = folded_barcode_text.as_ptr() as usize;
        for token in folded_barcode_text.split_whitespace() {
            if token.is_empty() {
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
            present_fields,
            search_text,
            search_lengths,
            barcode_entries,
            rate: row.rate,
            actual_qty: row.actual_qty,
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct IndexedItem {
    original_start: u32,
    original_lengths: [u32; ORIGINAL_FIELD_COUNT],
    present_fields: u8,
    search_start: u32,
    search_lengths: [u32; SEARCH_FIELD_COUNT],
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

#[derive(Default)]
pub struct CatalogIndex {
    items: Vec<IndexedItem>,
    original_text: String,
    search_text: String,
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
            self.index.items.push(IndexedItem {
                original_start,
                original_lengths: item.original_lengths,
                present_fields: item.present_fields,
                search_start,
                search_lengths: item.search_lengths,
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
        self.index.exact_barcode.shrink_to_fit();
        self.index.items.shrink_to_fit();
        self.index.original_text.shrink_to_fit();
        self.index.search_text.shrink_to_fit();
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

    fn original_range(item: &IndexedItem, field: usize) -> Range<usize> {
        let start = item.original_start as usize
            + item.original_lengths[..field]
                .iter()
                .map(|length| *length as usize)
                .sum::<usize>();
        start..start + item.original_lengths[field] as usize
    }

    fn original_field(&self, item: &IndexedItem, field: usize) -> Option<&str> {
        if item.present_fields & (1 << field) == 0 {
            return None;
        }
        Some(&self.original_text[Self::original_range(item, field)])
    }

    fn item_code(&self, item: &IndexedItem) -> &str {
        &self.original_text[Self::original_range(item, ITEM_CODE)]
    }

    fn search_range(item: &IndexedItem, field: usize) -> Range<usize> {
        let mut start = item.search_start as usize;
        for length in &item.search_lengths[..field] {
            start += *length as usize + SEARCH_SEPARATOR.len_utf8();
        }
        start..start + item.search_lengths[field] as usize
    }

    fn search_row_range(item: &IndexedItem) -> Range<usize> {
        let start = item.search_start as usize;
        let len = item
            .search_lengths
            .iter()
            .map(|length| *length as usize)
            .sum::<usize>()
            + SEARCH_FIELD_COUNT.saturating_sub(1) * SEARCH_SEPARATOR.len_utf8();
        start..start + len
    }

    fn search_field(&self, item: &IndexedItem, field: usize) -> &str {
        &self.search_text[Self::search_range(item, field)]
    }

    fn search_row_text(&self, item: &IndexedItem) -> &str {
        &self.search_text[Self::search_row_range(item)]
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
            modified: self.original_field(item, MODIFIED).map(str::to_owned),
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
                let barcode_range = Self::search_range(item, 4);
                let start = barcode_range.start + entry.start as usize;
                let end = barcode_range.start + entry.end as usize;
                self.search_text.get(start..end) == Some(folded_term)
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
                && memmem::find(
                    self.search_row_text(item).as_bytes(),
                    folded_term.as_bytes(),
                )
                .is_some()
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
    fn compact_row_metadata_stays_small() {
        assert!(std::mem::size_of::<IndexedItem>() <= 96);
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
