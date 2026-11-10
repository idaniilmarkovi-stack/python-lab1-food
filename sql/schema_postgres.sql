-- Схема в 3НФ (postgres); создаётся модулем src/db.py
CREATE TABLE data_sources (
  data_source_id INTEGER PRIMARY KEY,
  code VARCHAR(16) NOT NULL UNIQUE
);

CREATE TABLE countries (
  country_id INTEGER PRIMARY KEY,
  name VARCHAR(64) NOT NULL UNIQUE
);

CREATE TABLE serving_units (
  unit_id INTEGER PRIMARY KEY,
  code VARCHAR(8) NOT NULL UNIQUE
);

CREATE TABLE prep_states (
  prep_state_id INTEGER PRIMARY KEY,
  code VARCHAR(32) NOT NULL UNIQUE
);

CREATE TABLE brand_owners (
  brand_owner_id INTEGER PRIMARY KEY,
  name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE brands (
  brand_id INTEGER PRIMARY KEY,
  name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE subbrands (
  subbrand_id INTEGER PRIMARY KEY,
  name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE food_categories (
  category_id INTEGER PRIMARY KEY,
  name VARCHAR(1024) NOT NULL UNIQUE
);

CREATE TABLE trade_channels (
  channel_id INTEGER PRIMARY KEY,
  name VARCHAR(64) NOT NULL UNIQUE
);

CREATE TABLE branded_foods (
  fdc_id                      INTEGER PRIMARY KEY,
  gtin_upc                    VARCHAR(20) NOT NULL,
  description                 TEXT,
  brand_owner_id              INTEGER REFERENCES brand_owners(brand_owner_id),
  brand_id                    INTEGER REFERENCES brands(brand_id),
  subbrand_id                 INTEGER REFERENCES subbrands(subbrand_id),
  ingredients                 TEXT,
  not_a_significant_source_of TEXT,
  serving_size                REAL CHECK (serving_size IS NULL OR serving_size > 0),
  unit_id                     INTEGER REFERENCES serving_units(unit_id),
  serving_size_std            REAL CHECK (serving_size_std IS NULL OR serving_size_std > 0),
  household_serving_fulltext  VARCHAR(255),
  category_id                 INTEGER REFERENCES food_categories(category_id),
  data_source_id              INTEGER NOT NULL REFERENCES data_sources(data_source_id),
  package_weight              VARCHAR(64),
  package_weight_g            REAL CHECK (package_weight_g IS NULL OR package_weight_g > 0),
  package_weight_ml           REAL CHECK (package_weight_ml IS NULL OR package_weight_ml > 0),
  modified_date               DATE,
  available_date              DATE,
  publication_date            DATE,
  country_id                  INTEGER REFERENCES countries(country_id),
  discontinued_date           DATE,
  prep_state_id               INTEGER REFERENCES prep_states(prep_state_id),
  short_description           VARCHAR(64),
  material_code               VARCHAR(32)
);

CREATE TABLE food_trade_channels (
  fdc_id     INTEGER NOT NULL REFERENCES branded_foods(fdc_id),
  channel_id INTEGER NOT NULL REFERENCES trade_channels(channel_id),
  PRIMARY KEY (fdc_id, channel_id)
);
