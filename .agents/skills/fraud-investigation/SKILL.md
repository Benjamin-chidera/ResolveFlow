---
name: fraud-investigation
description: >-
  Analyze transaction risk, execute fraud investigation workflows, evaluate tabular ML features
  (XGBoost, DistilBERT, heuristics), and produce calibrated evidence-based risk reports.
---

# Fraud Investigation Skill

This skill guides the agent through fraud risk evaluation, anomaly detection, and event reconciliation.

## Investigation Protocol
1. **Signal Aggregation**:
   - Collect transaction metadata: velocity in last 1h/24h, amount relative to historical average, IP geolocation delta, device fingerprint shifts.
   - Aggregate counterparty risk score and prior return/chargeback history.
2. **ML Inference & Model Tiering**:
   - Query the tabular fraud classifier (XGBoost / LightGBM baseline).
   - Report calibrated probability, PR-AUC context, and feature contributions (SHAP values).
   - Evaluate against rule-based baselines (e.g. sanctions list match, high-risk country, sudden velocity spike).
3. **Evidence Synthesis**:
   - Correlate supporting documents (invoices, proof of delivery) fetched by the Evidence Retrieval Agent.
   - Distinguish between **hard indicators** (mismatched account numbers, velocity exhaustion) and **soft indicators** (slight location variance).
4. **Investigation Case Generation**:
   - Structure findings into an `InvestigationCase`:
     - `case_id`: UUID
     - `risk_score`: [0.0 - 1.0]
     - `risk_tier`: LOW / MEDIUM / HIGH / CRITICAL
     - `contributing_factors`: List of verified indicators
     - `recommended_action`: AUTO_APPROVE / STEP_UP_VERIFICATION / MANUAL_REVIEW / REJECT

