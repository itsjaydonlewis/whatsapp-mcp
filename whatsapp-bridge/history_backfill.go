package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

const (
	// historyBackfillMaxCount is the whatsmeow-recommended on-demand page size.
	// BuildHistorySyncRequest asks the primary phone for this many messages
	// immediately before a known anchor. It is not a lifetime history API.
	historyBackfillMaxCount = 50
	historyBackfillCoverage = "On-demand history sync asks the primary phone for at most 50 messages immediately before the oldest stored message in one chat. WhatsApp may return fewer, none, or report that older messages remain on the phone but are not available to a linked device. One call does not recover the entire lifetime history, and this repair never loops the request or resets the session."
)

// HistoryAnchor is the oldest stored message used to request older history.
// It carries no message body.
type HistoryAnchor struct {
	ID        string
	Timestamp time.Time
	IsFromMe  bool
}

// HistoryBackfillPlan describes one explicit history request.
type HistoryBackfillPlan struct {
	ChatJID      string `json:"chat_jid"`
	StoredCount  int    `json:"stored_count"`
	AnchorID     string `json:"anchor_id,omitempty"`
	AnchorUnix   int64  `json:"anchor_unix,omitempty"`
	AnchorFromMe bool   `json:"anchor_from_me"`
	RequestCount int    `json:"request_count"`
	CanRequest   bool   `json:"can_request"`
	BlockReason  string `json:"block_reason,omitempty"`
	Coverage     string `json:"coverage"`
	DryRun       bool   `json:"dry_run"`
}

type peerHistorySender interface {
	BuildHistorySyncRequest(lastKnownMessageInfo *types.MessageInfo, count int) *waE2E.Message
	SendPeerMessage(ctx context.Context, message *waE2E.Message) (whatsmeow.SendResponse, error)
}

func historyDryRun(flag *bool) bool {
	return flag == nil || *flag
}

func PlanHistoryBackfill(chatJID string, storedCount int, anchor *HistoryAnchor, requested int) HistoryBackfillPlan {
	count := requested
	if count <= 0 {
		count = historyBackfillMaxCount
	}
	if count > historyBackfillMaxCount {
		count = historyBackfillMaxCount
	}
	plan := HistoryBackfillPlan{
		ChatJID:      canonicalizeJIDString(chatJID),
		StoredCount:  storedCount,
		RequestCount: count,
		Coverage:     historyBackfillCoverage,
	}
	if storedCount <= 0 || anchor == nil || anchor.ID == "" || anchor.Timestamp.IsZero() {
		plan.CanRequest = false
		plan.BlockReason = "no stored anchor message; on-demand history sync requires the oldest stored message id, timestamp, and from-me flag. A chat with no stored messages cannot be backfilled without resetting the linked device, which this repair will not do."
		plan.RequestCount = 0
		return plan
	}
	plan.CanRequest = true
	plan.AnchorID = anchor.ID
	plan.AnchorUnix = anchor.Timestamp.Unix()
	plan.AnchorFromMe = anchor.IsFromMe
	return plan
}

func validateHistoryChatJID(chatJID string) error {
	canon := canonicalizeJIDString(chatJID)
	switch classifyJID(canon) {
	case kindPhone, kindLID, kindGroup:
		return nil
	default:
		return fmt.Errorf("history backfill only supports phone, lid, and group chats")
	}
}

// ExecuteHistoryBackfill sends one peer history request. Dry runs and
// impossible plans return before SendPeerMessage. This does not send a chat
// message, mark a chat read, or reset the session.
func ExecuteHistoryBackfill(ctx context.Context, sender peerHistorySender, plan HistoryBackfillPlan) error {
	if plan.DryRun {
		return fmt.Errorf("dry run: history request not sent")
	}
	if !plan.CanRequest {
		return fmt.Errorf("cannot request history: %s", plan.BlockReason)
	}
	if sender == nil {
		return fmt.Errorf("history sender is not available")
	}
	if err := validateHistoryChatJID(plan.ChatJID); err != nil {
		return err
	}
	chat, err := types.ParseJID(plan.ChatJID)
	if err != nil {
		return err
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chat.ToNonAD(),
			IsFromMe: plan.AnchorFromMe,
		},
		ID:        plan.AnchorID,
		Timestamp: time.Unix(plan.AnchorUnix, 0),
	}
	msg := sender.BuildHistorySyncRequest(info, plan.RequestCount)
	if msg == nil {
		return fmt.Errorf("history sync request could not be built")
	}
	_, err = sender.SendPeerMessage(ctx, msg)
	return err
}

func (store *MessageStore) HistoryAnchor(chatJID string) (int, *HistoryAnchor, error) {
	chatJID = canonicalizeJIDString(chatJID)
	var count int
	if err := store.db.QueryRow(`SELECT COUNT(*) FROM messages WHERE chat_jid = ?`, chatJID).Scan(&count); err != nil {
		return 0, nil, err
	}
	var id string
	var fromMe bool
	var raw any
	err := store.db.QueryRow(`
		SELECT id, timestamp, is_from_me
		FROM messages
		WHERE chat_jid = ? AND id != ''
		ORDER BY timestamp ASC, id ASC
		LIMIT 1
	`, chatJID).Scan(&id, &raw, &fromMe)
	if err == sql.ErrNoRows {
		return count, nil, nil
	}
	if err != nil {
		return count, nil, err
	}
	ts, err := parseDBTime(raw)
	if err != nil {
		return count, nil, err
	}
	return count, &HistoryAnchor{ID: id, Timestamp: ts, IsFromMe: fromMe}, nil
}

func parseDBTime(v any) (time.Time, error) {
	switch t := v.(type) {
	case time.Time:
		return t, nil
	case string:
		return parseTimeString(t)
	case []byte:
		return parseTimeString(string(t))
	default:
		return time.Time{}, fmt.Errorf("unsupported timestamp %T", v)
	}
}

func parseTimeString(s string) (time.Time, error) {
	layouts := []string{
		time.RFC3339Nano,
		time.RFC3339,
		"2006-01-02 15:04:05.999999999-07:00",
		"2006-01-02 15:04:05-07:00",
		"2006-01-02 15:04:05",
		"2006-01-02T15:04:05",
	}
	var last error
	for _, layout := range layouts {
		ts, err := time.Parse(layout, s)
		if err == nil {
			return ts, nil
		}
		last = err
	}
	return time.Time{}, last
}

type historyBackfillRequest struct {
	ChatJID   string  `json:"chat_jid"`
	Count     int     `json:"count"`
	DryRun    *bool   `json:"dry_run"`
	Message   *string `json:"message"`
	Recipient *string `json:"recipient"`
}

type historyBackfillResponse struct {
	Success  bool                `json:"success"`
	Executed bool                `json:"executed"`
	Message  string              `json:"message"`
	Plan     HistoryBackfillPlan `json:"plan"`
}

func registerRepairRoutes(client *whatsmeow.Client, store *MessageStore) {
	http.HandleFunc("/api/identities/refresh", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if client == nil || client.Store == nil || client.Store.Contacts == nil || client.Store.LIDs == nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": "contact store is not open"})
			return
		}
		summary, err := RefreshIdentityCache(r.Context(), client.Store.Contacts, client.Store.LIDs, store, nil)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "summary": summary})
	})

	http.HandleFunc("/api/identities/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		status, err := store.IdentityCacheStatus()
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"success": true, "status": status})
	})

	http.HandleFunc("/api/history-status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		chatJID := r.URL.Query().Get("chat_jid")
		w.Header().Set("Content-Type", "application/json")
		if err := validateHistoryChatJID(chatJID); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		count, anchor, err := store.HistoryAnchor(chatJID)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]any{"success": false, "message": err.Error()})
			return
		}
		plan := PlanHistoryBackfill(chatJID, count, anchor, historyBackfillMaxCount)
		plan.DryRun = true
		json.NewEncoder(w).Encode(map[string]any{"success": true, "plan": plan})
	})

	http.HandleFunc("/api/history-backfill", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req historyBackfillRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if req.Message != nil || req.Recipient != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(historyBackfillResponse{
				Success: false,
				Message: "this endpoint does not send chat messages",
			})
			return
		}
		dry := historyDryRun(req.DryRun)
		if err := validateHistoryChatJID(req.ChatJID); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(historyBackfillResponse{Success: false, Message: err.Error()})
			return
		}
		count, anchor, err := store.HistoryAnchor(req.ChatJID)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(historyBackfillResponse{Success: false, Message: err.Error()})
			return
		}
		plan := PlanHistoryBackfill(req.ChatJID, count, anchor, req.Count)
		plan.DryRun = dry
		if dry {
			json.NewEncoder(w).Encode(historyBackfillResponse{
				Success:  true,
				Executed: false,
				Message:  "Dry run only. No history request was sent.",
				Plan:     plan,
			})
			return
		}
		if !plan.CanRequest {
			json.NewEncoder(w).Encode(historyBackfillResponse{
				Success:  false,
				Executed: false,
				Message:  plan.BlockReason,
				Plan:     plan,
			})
			return
		}
		if err := ExecuteHistoryBackfill(r.Context(), client, plan); err != nil {
			w.WriteHeader(http.StatusBadGateway)
			json.NewEncoder(w).Encode(historyBackfillResponse{
				Success:  false,
				Executed: false,
				Message:  err.Error(),
				Plan:     plan,
			})
			return
		}
		json.NewEncoder(w).Encode(historyBackfillResponse{
			Success:  true,
			Executed: true,
			Message:  "One on-demand history request was sent to this account's primary device. The phone decides how many older messages, if any, come back. This is not a full history recovery.",
			Plan:     plan,
		})
	})
}
