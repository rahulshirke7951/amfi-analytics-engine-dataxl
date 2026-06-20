# 🏗️ Architecture — AMFI Analytics Engine

## Overview
This document explains how data flows through the system: ingestion → processing → analytics → consumption.

---

## 🧠 High-Level Architecture

```mermaid
flowchart LR
    A[AMFI / External Sources] --> B[Ingestion Layer]
    B --> C[Raw Data Storage]
    C --> D[Processing Layer]
    D --> E[Curated Data]
    E --> F[Analytics Engine]
    F --> G[Consumers]
```

---

## 🔄 Data Flow

```mermaid
flowchart TD
    A[Scheduler] --> B[Fetch Data]
    B --> C[Raw Storage]
    C --> D[Cleaning]
    D --> E[Normalization]
    E --> F[Processed Data]
    F --> G[Analytics]
    G --> H[Outputs]
```

---

## 📥 Ingestion
- Fetch NAV data
- Pull metadata
- Store raw files

---

## 🧹 Processing
- Clean missing values
- Normalize schema
- Validate datasets

---

## 📊 Analytics
- Returns calculation
- Fund ranking
- Category performance

---

## 📤 Outputs
- CSV / JSON
- API-ready data
- Dashboard datasets

---

## 🧩 Future Enhancements
- Real-time ingestion
- Risk metrics
- ML-based predictions
