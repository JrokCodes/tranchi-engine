// ─── API: Sources ──────────────────────────────────────────────────────────────

export interface ApiSource {
  source_site: string;
  status: string;
  online: boolean;
  started_at: string;
  completed_at: string | null;
  minutes_since: number;
  found: number;
  passed: number;
  active: number;
  filtered: number;
  dupes: number;
  delisted: number;
  expired: number;
  new_today: number;
  error_message: string | null;
  source_url: string | null;
  category: 'deal' | 'signal' | 'registry';
}

export interface SourcesResponse {
  sources: ApiSource[];
}

// ─── API: Listings ─────────────────────────────────────────────────────────────

export type ListingStatus = 'active' | 'not_listed' | 'expired' | 'cancelled';

export interface ApiListingItem {
  id: string;
  source_site: string;
  signal_type: string;
  property_address: string;
  property_city: string;
  property_county: string;
  property_state: string;
  property_zip: string;
  status: ListingStatus;
  sale_date: string | null;
  sec_sale_date: string | null;
  deposit_usd: number | null;
  opening_bid_usd: number | null;
  appraised_value_usd: number | null;
  auction_status: string | null;
  trustee_name: string | null;
  case_number: string | null;
  source_listing_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  signal_count: number;
  signal_types: { label: string; count: number }[];
  signal_type_count: number;
  is_hot: boolean;
  owner_name: string | null;
  situs_address: string | null;
  current_market_value: number | null;
  current_tax_balance: number | null;
  delinquent_flag: boolean;
  // Google Street View image URL (null when no Maps key / no coverage).
  street_view_url: string | null;
}

export interface ListingsResponse {
  items: ApiListingItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// ─── API: Listing Detail ───────────────────────────────────────────────────────

export interface ApiParcel {
  parcel_number: string | null;
  owner_name: string | null;
  situs_address: string | null;
  owner_mailing_address: string | null;
  current_market_value: number | null;
  taxable_value: number | null;
  current_tax_balance: number | null;
  delinquent_flag: boolean;
  year_built: number | null;
  sq_ft: number | null;
  beds: number | null;
  baths: number | null;
  last_sale_date: string | null;
  last_sale_price: number | null;
  source_url: string | null;
}

export interface ApiSignal {
  signal_type: string;
  source: string;
  observed_at: string;
  confidence: number;
  payload: Record<string, unknown>;
}

export interface ApiListingDetail {
  listing: ApiListingItem;
  parcel: ApiParcel | null;
  signals: ApiSignal[];
}
