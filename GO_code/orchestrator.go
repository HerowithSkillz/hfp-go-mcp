package main

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	_ "github.com/lib/pq"           // Postgres Driver
	_ "github.com/mattn/go-sqlite3" // SQLite Driver
)

// --- CONFIGURATION ---
var (
	// Local = "sqlite3", Server = "postgres"
	DBDriver = getEnv("DB_DRIVER", "sqlite3")

	// Network: Local = "tcp" (:8080), Server = "unix" (/tmp/sock)
	NetworkType = getEnv("NET_TYPE", "tcp")
	NetworkAddr = getEnv("NET_ADDR", ":8095")

	// Target: Where to forward traffic?
	// CHANGED: Uses AI_NODES to match your terminal command
	TargetURL = getEnv("AI_NODES", "http://127.0.0.1:8085")

	// Postgres Settings
	DB_HOST     = getEnv("DB_HOST", "localhost")
	DB_PORT     = getEnv("DB_PORT", "5432")
	DB_USER     = getEnv("DB_USER", "postgres")
	DB_PASSWORD = getEnv("DB_PASSWORD", "password")
	DB_NAME     = getEnv("DB_NAME", "postgres")
)

var db *sql.DB

type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

func main() {
	log.Printf("⚙️  Mode: %s | DB: %s", NetworkType, DBDriver)
	log.Printf("🎯 Forwarding to: %s", TargetURL)

	// 1. CONNECT TO DB
	initDB()

	// 2. START LISTENER
	var listener net.Listener
	var err error

	if NetworkType == "unix" {
		if _, err := os.Stat(NetworkAddr); err == nil {
			os.Remove(NetworkAddr)
		}
		listener, err = net.Listen("unix", NetworkAddr)
		os.Chmod(NetworkAddr, 0777)
	} else {
		listener, err = net.Listen("tcp", NetworkAddr)
	}

	if err != nil {
		log.Fatalf("❌ Failed to bind: %v", err)
	}

	log.Printf("🟢 Go Brain running on %s", NetworkAddr)
	http.Serve(listener, http.HandlerFunc(handleProxy))
}
func handleProxy(w http.ResponseWriter, r *http.Request) {
	// 1. LOG EVERYTHING (Don't block anything)
	log.Printf("📩 Request: %s [%s]", r.URL.Path, r.Method)

	// 2. READ BODY (Need it for logging OR forwarding)
	bodyBytes, _ := io.ReadAll(r.Body)
	r.Body.Close()

	// 3. CHECK: Is this a Chat? (Only log chats to DB)
	isChat := (r.Method == "POST" && strings.Contains(r.URL.Path, "/chat/completions"))
	userID := r.Header.Get("X-Request-ID")
	if userID == "" {
		userID = "unknown"
	}

	// 4. IF CHAT: SAVE PROMPT
	if isChat {
		var reqPayload map[string]interface{}
		if err := json.Unmarshal(bodyBytes, &reqPayload); err == nil {
			messages_raw, _ := json.Marshal(reqPayload["messages"])
			var messages []ChatMessage
			json.Unmarshal(messages_raw, &messages)

			if len(messages) > 0 {
				lastMsg := messages[len(messages)-1]
				if lastMsg.Role == "user" {
					log.Printf("📝 Logging User Prompt...")
					go saveToDB(userID, "user", lastMsg.Content)
				}
			}
		}
	}

	// 5. FORWARD EVERYTHING (Models, Health, Chats - all of it!)
	// Use r.Method so GET requests (like /models) work too
	proxyReq, _ := http.NewRequest(r.Method, TargetURL+r.URL.Path, bytes.NewBuffer(bodyBytes))
	proxyReq.Header = r.Header

	client := &http.Client{Timeout: 120 * time.Second}
	resp, err := client.Do(proxyReq)
	if err != nil {
		log.Printf("❌ Upstream Error: %v", err)
		http.Error(w, "AI Cluster Offline", 502)
		return
	}
	defer resp.Body.Close()

	// 6. RETURN RESPONSE
	copyHeader(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)

	// 7. IF CHAT: CAPTURE RESPONSE (Otherwise just stream it)
	var responseBuffer bytes.Buffer
	var outputWriter io.Writer

	if isChat {
		outputWriter = io.MultiWriter(w, &responseBuffer)
	} else {
		outputWriter = w
	}

	io.Copy(outputWriter, resp.Body)

	// 8. SAVE RESPONSE TO DB (Only if it was a chat)
	if isChat {
		go saveToDB(userID, "assistant", responseBuffer.String())
	}
}

// --- DATABASE HELPERS ---
func initDB() {
	var err error
	if DBDriver == "sqlite3" {
		db, err = sql.Open("sqlite3", "./local_chat.db")
		if err == nil {
			log.Println("📂 Using SQLite (Local File)")
			query := `CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );`
			db.Exec(query)
		}
	} else {
		psqlInfo := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
			DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME)
		db, err = sql.Open("postgres", psqlInfo)
		if err == nil {
			log.Println("🐘 Using PostgreSQL (Server)")
			query := `CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                session_id TEXT, role TEXT, content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );`
			db.Exec(query)
		}
	}

	if err != nil {
		log.Fatal(err)
	}
}

func saveToDB(sid, role, content string) {
	var query string
	if DBDriver == "sqlite3" {
		query = `INSERT INTO chat_history (session_id, role, content) VALUES (?, ?, ?)`
	} else {
		query = `INSERT INTO chat_history (session_id, role, content) VALUES ($1, $2, $3)`
	}
	db.Exec(query, sid, role, content)
}

func getHistory(sid string) []ChatMessage {
	var query string
	if DBDriver == "sqlite3" {
		query = "SELECT role, content FROM chat_history WHERE session_id = ? ORDER BY id DESC LIMIT 10"
	} else {
		query = "SELECT role, content FROM chat_history WHERE session_id = $1 ORDER BY id DESC LIMIT 10"
	}

	rows, _ := db.Query(query, sid)
	if rows != nil {
		defer rows.Close()
	} else {
		return []ChatMessage{}
	}

	var history []ChatMessage
	for rows.Next() {
		var msg ChatMessage
		rows.Scan(&msg.Role, &msg.Content)
		history = append([]ChatMessage{msg}, history...)
	}
	// Reverse
	for i, j := 0, len(history)-1; i < j; i, j = i+1, j-1 {
		history[i], history[j] = history[j], history[i]
	}
	return history
}

func getEnv(key, fallback string) string {
	if value, exists := os.LookupEnv(key); exists {
		return value
	}
	return fallback
}

func copyHeader(dst, src http.Header) {
	for k, vv := range src {
		for _, v := range vv {
			dst.Add(k, v)
		}
	}
}
