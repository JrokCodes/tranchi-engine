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
  signal_type: string | null;
  property_address: string;
  property_city: string | null;
  property_county: string | null;
  property_state: string;
  property_zip: string | null;
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
  // buy_now (acquirable deal) | distress_signal (pre-distress lead). Drives the stage view.
  distress_stage: string | null;
  // Probate validity + decedent identity (null for non-probate; surfaced for the probate card).
  case_status: string | null;
  case_status_date: string | null;
  match_method: string | null;
  match_confidence: string | null;
  match_score: number | null;
  decedent_name: string | null;
  case_title: string | null;
  decedent_dod: string | null;
  // TN tax-deed redemption lifecycle (null for non-tax_deed; dormant until parcels sell).
  confirmation_order_date: string | null;
  redemption_ends: string | null;
  redemption_status: string | null;
  redemption_window_days: number | null;
  redemption_basis: string | null;
  address_status: string | null;
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
  // One-click verification deep-links (built server-side in verify_links.py).
  verify_links: VerifyLinks | null;
  // Blight pre-distress conviction tier (Wayne–Detroit only; null on all other listings).
  // A = highest conviction, C = watch. Derived server-side from blight_ticket_count +
  // blight_total_balance + absentee_owner. Non-null only when distress_stage=distress_signal.
  conviction_tier: 'A' | 'B' | 'C' | null;
  blight_ticket_count: number | null;
  blight_total_balance: number | null;
  absentee_owner: boolean | null;
  // Owner is a business/investor entity (LLC/Inc/Capital/Properties/Realty…), derived server-side
  // from the county owner_name. Surfaces the "Investor" badge + the Individuals-only filter on
  // pre-distress leads. Distinct from absentee_owner. Null when owner unknown.
  owner_is_entity: boolean | null;
}

export interface VerifyLinks {
  zillow: string | null;
  redfin: string | null;
  registry: string | null;
  registry_label: string | null;
  source: string | null;
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
  native_parcel_id: string | null;
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
