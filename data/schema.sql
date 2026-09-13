CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, account_name VARCHAR NOT NULL, currency VARCHAR NOT NULL, timezone VARCHAR NOT NULL);
CREATE TABLE campaigns (campaign_id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(account_id), campaign_name VARCHAR NOT NULL);
CREATE TABLE ad_groups (ad_group_id INTEGER PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(campaign_id), ad_group_name VARCHAR NOT NULL);
CREATE TABLE creatives (creative_id INTEGER PRIMARY KEY, ad_group_id INTEGER REFERENCES ad_groups(ad_group_id), creative_name VARCHAR NOT NULL);
CREATE TABLE channels (channel_id INTEGER PRIMARY KEY, channel_name VARCHAR NOT NULL);
CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region_name VARCHAR NOT NULL);
CREATE TABLE devices (device_id INTEGER PRIMARY KEY, device_name VARCHAR NOT NULL);
CREATE TABLE daily_metrics (
  date DATE NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(account_id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(campaign_id),
  ad_group_id INTEGER NOT NULL REFERENCES ad_groups(ad_group_id),
  creative_id INTEGER NOT NULL REFERENCES creatives(creative_id),
  channel_id INTEGER NOT NULL REFERENCES channels(channel_id),
  region_id INTEGER NOT NULL REFERENCES regions(region_id),
  device_id INTEGER NOT NULL REFERENCES devices(device_id),
  impressions BIGINT NOT NULL CHECK (impressions >= 0),
  clicks BIGINT NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
  spend DOUBLE NOT NULL CHECK (spend >= 0),
  conversions BIGINT NOT NULL CHECK (conversions >= 0 AND conversions <= clicks),
  conversion_value DOUBLE NOT NULL CHECK (conversion_value >= 0),
  daily_budget DOUBLE NOT NULL CHECK (daily_budget >= 0),
  budget_limited INTEGER NOT NULL CHECK (budget_limited IN (0, 1)),
  PRIMARY KEY (date, campaign_id, creative_id, channel_id, region_id, device_id)
);
