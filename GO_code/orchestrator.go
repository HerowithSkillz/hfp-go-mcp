package main

import (
	"bytes"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/smtp"
	"os"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	_ "github.com/lib/pq"           // Postgres Driver
	_ "github.com/mattn/go-sqlite3" // SQLite Driver
	"golang.org/x/crypto/bcrypt"
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
	TargetURL = getEnv("AI_NODES", "https://dev-ai.nomineelife.com:8085")

	// Postgres Settings
	DB_HOST     = getEnv("DB_HOST", "localhost")
	DB_PORT     = getEnv("DB_PORT", "5432")
	DB_USER     = getEnv("DB_USER", "postgres")
	DB_PASSWORD = getEnv("DB_PASSWORD", "password")
	DB_NAME     = getEnv("DB_NAME", "postgres")

	// JWT Secret (MUST be set in production)
	JWTSecret = getEnv("JWT_SECRET", "hfp-dev-secret-change-me-in-production")

	// SMTP Settings for Forgot Password
	SMTP_HOST = getEnv("SMTP_HOST", "")
	SMTP_PORT = getEnv("SMTP_PORT", "587")
	SMTP_USER = getEnv("SMTP_USER", "")
	SMTP_PASS = getEnv("SMTP_PASS", "")
	SMTP_FROM = getEnv("SMTP_FROM", "noreply@healthfirstpriority.com")

	// Frontend URL for reset links
	FRONTEND_URL = getEnv("FRONTEND_URL", "https://dev-ai.nomineelife.com")
)

var db *sql.DB

type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// --- AUTH TYPES ---
type UserResponse struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	Email string `json:"email"`
}

type AuthResponse struct {
	User  UserResponse `json:"user"`
	Token string       `json:"token"`
}

type JWTClaims struct {
	UserID string `json:"user_id"`
	Email  string `json:"email"`
	jwt.RegisteredClaims
}

func main() {
	log.Printf("⚙️  Mode: %s | DB: %s", NetworkType, DBDriver)
	log.Printf("🎯 Forwarding to: %s", TargetURL)

	// 1. CONNECT TO DB
	initDB()

	// 2. SET UP ROUTER
	mux := http.NewServeMux()

	// Auth routes (handled FIRST, before proxy catch-all)
	mux.HandleFunc("/api/auth/signup", handleCORS(handleSignup))
	mux.HandleFunc("/api/auth/login", handleCORS(handleLogin))
	mux.HandleFunc("/api/auth/forgot-password", handleCORS(handleForgotPassword))
	mux.HandleFunc("/api/auth/reset-password", handleCORS(handleResetPassword))

	// Everything else → original proxy handler (unchanged)
	mux.HandleFunc("/", handleProxy)

	// 3. START LISTENER
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
	log.Printf("🔐 Auth endpoints active at /api/auth/*")
	http.Serve(listener, mux)
}

// =============================================================================
// CORS MIDDLEWARE (for frontend fetch calls)
// =============================================================================
func handleCORS(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")

		if r.Method == "OPTIONS" {
			w.WriteHeader(204)
			return
		}
		next(w, r)
	}
}

// =============================================================================
// ORIGINAL PROXY HANDLER (100% UNCHANGED)
// =============================================================================
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

// =============================================================================
// AUTH HANDLERS
// =============================================================================

func handleSignup(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		jsonError(w, "Method not allowed", 405)
		return
	}

	var req struct {
		Name     string `json:"name"`
		Email    string `json:"email"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", 400)
		return
	}

	// Validate
	req.Email = strings.TrimSpace(strings.ToLower(req.Email))
	req.Name = strings.TrimSpace(req.Name)
	if req.Name == "" || req.Email == "" || req.Password == "" {
		jsonError(w, "Name, email, and password are required", 400)
		return
	}
	if len(req.Password) < 6 {
		jsonError(w, "Password must be at least 6 characters", 400)
		return
	}

	// Check if email already exists
	var existingID string
	var checkQuery string
	if DBDriver == "sqlite3" {
		checkQuery = "SELECT id FROM users WHERE email = ?"
	} else {
		checkQuery = "SELECT id FROM users WHERE email = $1"
	}
	err := db.QueryRow(checkQuery, req.Email).Scan(&existingID)
	if err == nil {
		jsonError(w, "An account with this email already exists", 409)
		return
	}

	// Hash password
	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), bcrypt.DefaultCost)
	if err != nil {
		log.Printf("❌ Bcrypt error: %v", err)
		jsonError(w, "Internal server error", 500)
		return
	}

	// Generate user ID
	userID := generateID()

	// Insert user
	var insertQuery string
	if DBDriver == "sqlite3" {
		insertQuery = "INSERT INTO users (id, name, email, password_hash) VALUES (?, ?, ?, ?)"
	} else {
		insertQuery = "INSERT INTO users (id, name, email, password_hash) VALUES ($1, $2, $3, $4)"
	}
	_, err = db.Exec(insertQuery, userID, req.Name, req.Email, string(hash))
	if err != nil {
		log.Printf("❌ DB insert error: %v", err)
		jsonError(w, "Failed to create account", 500)
		return
	}

	// Generate JWT
	token, err := generateToken(userID, req.Email)
	if err != nil {
		log.Printf("❌ JWT error: %v", err)
		jsonError(w, "Internal server error", 500)
		return
	}

	log.Printf("✅ New user registered: %s (%s)", req.Name, req.Email)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(AuthResponse{
		User:  UserResponse{ID: userID, Name: req.Name, Email: req.Email},
		Token: token,
	})
}

func handleLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		jsonError(w, "Method not allowed", 405)
		return
	}

	var req struct {
		Email    string `json:"email"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", 400)
		return
	}

	req.Email = strings.TrimSpace(strings.ToLower(req.Email))
	if req.Email == "" || req.Password == "" {
		jsonError(w, "Email and password are required", 400)
		return
	}

	// Fetch user
	var userID, name, email, passwordHash string
	var fetchQuery string
	if DBDriver == "sqlite3" {
		fetchQuery = "SELECT id, name, email, password_hash FROM users WHERE email = ?"
	} else {
		fetchQuery = "SELECT id, name, email, password_hash FROM users WHERE email = $1"
	}
	err := db.QueryRow(fetchQuery, req.Email).Scan(&userID, &name, &email, &passwordHash)
	if err != nil {
		jsonError(w, "Invalid email or password", 401)
		return
	}

	// Verify password
	if err := bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.Password)); err != nil {
		jsonError(w, "Invalid email or password", 401)
		return
	}

	// Generate JWT
	token, err := generateToken(userID, email)
	if err != nil {
		log.Printf("❌ JWT error: %v", err)
		jsonError(w, "Internal server error", 500)
		return
	}

	log.Printf("✅ User logged in: %s (%s)", name, email)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(AuthResponse{
		User:  UserResponse{ID: userID, Name: name, Email: email},
		Token: token,
	})
}

func handleForgotPassword(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		jsonError(w, "Method not allowed", 405)
		return
	}

	var req struct {
		Email string `json:"email"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", 400)
		return
	}

	req.Email = strings.TrimSpace(strings.ToLower(req.Email))
	if req.Email == "" {
		jsonError(w, "Email is required", 400)
		return
	}

	// Always respond with success to prevent email enumeration
	successMsg := map[string]string{"message": "If an account with that email exists, a password reset link has been sent."}

	// Check if user exists
	var userID string
	var checkQuery string
	if DBDriver == "sqlite3" {
		checkQuery = "SELECT id FROM users WHERE email = ?"
	} else {
		checkQuery = "SELECT id FROM users WHERE email = $1"
	}
	err := db.QueryRow(checkQuery, req.Email).Scan(&userID)
	if err != nil {
		// User doesn't exist, but don't reveal that
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(successMsg)
		return
	}

	// Generate reset token (32-byte hex)
	resetToken := generateResetToken()
	expiry := time.Now().Add(1 * time.Hour) // 1 hour validity

	// Save to DB
	var updateQuery string
	if DBDriver == "sqlite3" {
		updateQuery = "UPDATE users SET reset_token = ?, reset_expiry = ? WHERE id = ?"
		db.Exec(updateQuery, resetToken, expiry.Format(time.RFC3339), userID)
	} else {
		updateQuery = "UPDATE users SET reset_token = $1, reset_expiry = $2 WHERE id = $3"
		db.Exec(updateQuery, resetToken, expiry, userID)
	}

	// Build reset link
	resetLink := fmt.Sprintf("%s/reset-password?token=%s", FRONTEND_URL, resetToken)

	// Send email if SMTP is configured, otherwise log
	if SMTP_HOST != "" && SMTP_USER != "" {
		go sendResetEmail(req.Email, resetLink)
	} else {
		log.Printf("📧 [NO SMTP] Reset link for %s: %s", req.Email, resetLink)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(successMsg)
}

func handleResetPassword(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		jsonError(w, "Method not allowed", 405)
		return
	}

	var req struct {
		Token    string `json:"token"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", 400)
		return
	}

	if req.Token == "" || req.Password == "" {
		jsonError(w, "Token and new password are required", 400)
		return
	}
	if len(req.Password) < 6 {
		jsonError(w, "Password must be at least 6 characters", 400)
		return
	}

	// Find user by reset token
	var userID, resetExpiry string
	var fetchQuery string
	if DBDriver == "sqlite3" {
		fetchQuery = "SELECT id, reset_expiry FROM users WHERE reset_token = ?"
	} else {
		fetchQuery = "SELECT id, reset_expiry FROM users WHERE reset_token = $1"
	}
	err := db.QueryRow(fetchQuery, req.Token).Scan(&userID, &resetExpiry)
	if err != nil {
		jsonError(w, "Invalid or expired reset token", 400)
		return
	}

	// Check expiry
	expiry, err := time.Parse(time.RFC3339, resetExpiry)
	if err != nil || time.Now().After(expiry) {
		jsonError(w, "Reset token has expired. Please request a new one.", 400)
		return
	}

	// Hash new password
	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), bcrypt.DefaultCost)
	if err != nil {
		jsonError(w, "Internal server error", 500)
		return
	}

	// Update password and clear reset token
	var updateQuery string
	if DBDriver == "sqlite3" {
		updateQuery = "UPDATE users SET password_hash = ?, reset_token = NULL, reset_expiry = NULL WHERE id = ?"
	} else {
		updateQuery = "UPDATE users SET password_hash = $1, reset_token = NULL, reset_expiry = NULL WHERE id = $2"
	}
	db.Exec(updateQuery, string(hash), userID)

	log.Printf("✅ Password reset successful for user %s", userID)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"message": "Password has been reset successfully. You can now log in."})
}

// =============================================================================
// JWT HELPERS
// =============================================================================

func generateToken(userID, email string) (string, error) {
	claims := JWTClaims{
		UserID: userID,
		Email:  email,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(7 * 24 * time.Hour)), // 7 days
			IssuedAt:  jwt.NewNumericDate(time.Now()),
			Issuer:    "healthfirstpriority",
		},
	}

	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	return token.SignedString([]byte(JWTSecret))
}

func validateToken(tokenStr string) (*JWTClaims, error) {
	token, err := jwt.ParseWithClaims(tokenStr, &JWTClaims{}, func(token *jwt.Token) (interface{}, error) {
		if _, ok := token.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", token.Header["alg"])
		}
		return []byte(JWTSecret), nil
	})
	if err != nil {
		return nil, err
	}

	claims, ok := token.Claims.(*JWTClaims)
	if !ok || !token.Valid {
		return nil, fmt.Errorf("invalid token")
	}
	return claims, nil
}

// =============================================================================
// UTILITY HELPERS
// =============================================================================

func generateID() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func generateResetToken() string {
	b := make([]byte, 32)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func jsonError(w http.ResponseWriter, message string, statusCode int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	json.NewEncoder(w).Encode(map[string]string{"error": message})
}

func sendResetEmail(toEmail, resetLink string) {
	subject := "Reset Your HealthFirstPriority Password"
	body := fmt.Sprintf(
		"Hello,\n\nYou requested a password reset. Click the link below to set a new password:\n\n%s\n\nThis link expires in 1 hour.\n\nIf you didn't request this, you can safely ignore this email.\n\n— HealthFirstPriority Team",
		resetLink,
	)

	msg := fmt.Sprintf("From: %s\r\nTo: %s\r\nSubject: %s\r\n\r\n%s", SMTP_FROM, toEmail, subject, body)

	auth := smtp.PlainAuth("", SMTP_USER, SMTP_PASS, SMTP_HOST)
	addr := fmt.Sprintf("%s:%s", SMTP_HOST, SMTP_PORT)

	err := smtp.SendMail(addr, auth, SMTP_FROM, []string{toEmail}, []byte(msg))
	if err != nil {
		log.Printf("❌ Failed to send reset email to %s: %v", toEmail, err)
		log.Printf("📧 [FALLBACK] Reset link: %s", resetLink)
	} else {
		log.Printf("📧 Reset email sent to %s", toEmail)
	}
}

// =============================================================================
// DATABASE HELPERS (ORIGINAL + users table)
// =============================================================================
func initDB() {
	var err error
	if DBDriver == "sqlite3" {
		db, err = sql.Open("sqlite3", "./local_chat.db")
		if err == nil {
			log.Println("📂 Using SQLite (Local File)")

			// Original chat_history table (UNCHANGED)
			query := `CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );`
			db.Exec(query)

			// New users table
			usersQuery := `CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                reset_token TEXT,
                reset_expiry TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );`
			db.Exec(usersQuery)
		}
	} else {
		psqlInfo := fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=disable",
			DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME)
		db, err = sql.Open("postgres", psqlInfo)
		if err == nil {
			log.Println("🐘 Using PostgreSQL (Server)")

			// Original chat_history table (UNCHANGED)
			query := `CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                session_id TEXT, role TEXT, content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );`
			db.Exec(query)

			// New users table
			usersQuery := `CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                reset_token TEXT,
                reset_expiry TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );`
			db.Exec(usersQuery)
		}
	}

	if err != nil {
		log.Fatal(err)
	}
}

// ORIGINAL saveToDB (UNCHANGED)
func saveToDB(sid, role, content string) {
	var query string
	if DBDriver == "sqlite3" {
		query = `INSERT INTO chat_history (session_id, role, content) VALUES (?, ?, ?)`
	} else {
		query = `INSERT INTO chat_history (session_id, role, content) VALUES ($1, $2, $3)`
	}
	db.Exec(query, sid, role, content)
}

// ORIGINAL getHistory (UNCHANGED)
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

// ORIGINAL getEnv (UNCHANGED)
func getEnv(key, fallback string) string {
	if value, exists := os.LookupEnv(key); exists {
		return value
	}
	return fallback
}

// ORIGINAL copyHeader (UNCHANGED)
func copyHeader(dst, src http.Header) {
	for k, vv := range src {
		for _, v := range vv {
			dst.Add(k, v)
		}
	}
}
