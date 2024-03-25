package main

import (
	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	"log"
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
)

func main() {

	token, err := readBotToken("./config/token.txt")
	if err != nil {
		log.Panicf("Token error: ", err)
	}
	bot, err := tgbotapi.NewBotAPI(token)
	if err != nil {
		log.Panic(err)
	}

	log.Printf("Authorized on account %s", bot.Self.UserName)
	
	db, err := sql.Open("sqlite3", "./mydb.db")
	if err != nil {
		log.Println(err)
		return
	}
	defer db.Close()

	// ADD DATABASE CODE

	u := tgbotapi.NewUpdate(0)
	u.Timeout = 60

	updates := bot.GetUpdatesChan(u)
	

	for update := range updates {

		if update.Message != nil {
			m := update.Message
			if m.From == nil {
				continue
			}

			log.Printf("Message received")
			err := handleMessage(bot, m, db)
			if err != nil {
				log.Printf("[%s] %s,   err: %s", update.Message.From.UserName, update.Message.Text, err.Error())
				continue
			}

			log.Printf("[%s] %s", update.Message.From.UserName, update.Message.Text)
		}
	}
}
