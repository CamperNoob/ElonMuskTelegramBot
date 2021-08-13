import threading
from time import sleep
import logging
import logging.handlers
import tweepy
import telegram.ext
import telegram
from os import path, mkdir
from typing import Union
# tokens
# TODO: change to json tokens
from tokens import TelegramTOKEN, TwitterTOKEN

# constants
elonID = '44196397'
latest_tweet_file = "data\\latesttweetid.dat"
subscribers_file = "data\\subscriptions.dat"
twitter_auth = tweepy.OAuthHandler(TwitterTOKEN['ckey'], TwitterTOKEN['csecret'])
twitter_auth.set_access_token(TwitterTOKEN['at'], TwitterTOKEN['atsecret'])


# decorator function for recursive error handling
def recursive_handler(func: callable(object)) -> callable(object):
    def inner(*args, **kwargs):
        # try instantly execute a function and return it's result
        try:
            return func(*args, **kwargs)
        # if exception is raised - do something with it and execute this function again
        except Exception as e:
            # do something with the exception, e.g. sleep(300) when exception is TooManyRequests
            logger.exception(f"Exception caught: {e}")
            sleep(300)
            # return the value of successful execution to the higher recursion instance (without return - passes None)
            return inner(*args, **kwargs)
    return inner


# tweet class parser
class ParsedTweetToTelegram:
    def __init__(self, tweet: tweepy.models.Status):
        try:
            self.media = tweet.entities['media'][0]['media_url']
            self.is_media = True
        except KeyError:
            self.is_media = False
        self.tweet_id = tweet.id
        self.tweet_url = f'https://twitter.com/user/status/{self.tweet_id}'
        self.text = tweet.full_text
        self.created_at = tweet.created_at.strftime("%H:%M(UTC) · %Y-%m-%d")

    def string(self) -> str:
        return f"Elon Musk:\n\n{self.text}\n\n{self.created_at}"


# file jobs
# TODO: change to json subscribers
def subscribers_file_read(file) -> list:
    subscriber_list = []
    if not path.exists(file):
        if not path.exists("data"):
            mkdir("data")
        open(file, "x").close()
    subscribed = open(file, 'r')
    for _user in subscribed:
        subscriber_list.append(int(_user.rstrip("\n")))
    subscribed.close()
    return subscriber_list


# TODO: change to json latest and add fallback
def latest_tweet_id_file_read(file) -> int:
    _latest_tweet_id = []
    if not path.exists(file):
        if not path.exists("data"):
            mkdir("data")
        open(file, "x").close()
    _file = open(file, 'r')
    for _tweet_id in _file:
        _latest_tweet_id.append(int(_tweet_id.rstrip("\n")))
    _file.close()
    # fallback default value
    if not _latest_tweet_id:
        return 1412818236203102214
    return _latest_tweet_id[0]


def write_latest_tweet(_latest_tweet_file: str, tweet: ParsedTweetToTelegram) -> None:
    open(_latest_tweet_file, 'w').close()
    with open(_latest_tweet_file, 'a') as file:
        file.write(str(tweet.tweet_id))
        file.close()


# logger config
def logger_config(_logger: logging.Logger, is_debug: False) -> None:
    if not is_debug:
        def namer(name):
            return name.replace(".log", "") + ".log"

        handler = logging.handlers.TimedRotatingFileHandler(
            filename='logs/log.log',
            when='midnight', interval=1
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        handler.suffix = '%Y-%m-%d'
        handler.namer = namer
        _logger.addHandler(handler)
        _logger.setLevel(logging.DEBUG)
    else:
        logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                            level=logging.DEBUG)


# Background threaded updater of subscribers file
def update_subscribers():
    __subscriptions__ = subscribers_file_read(subscribers_file)
    while True:
        logger.debug("Started subscriptions file job")
        sleep(60)
        if not __subscriptions__ == subscriptions:
            logger.info("Updating subscriptions file, difference: "
                        f"{list(set(__subscriptions__).symmetric_difference(set(subscriptions)))}")
            __subscriptions__ = subscriptions
            open(subscribers_file, 'w').close()
            with open(subscribers_file, 'a') as file:
                file.write('\n'.join(str(user) for user in __subscriptions__))
                file.close()


# Background threaded messenger
def newsletter() -> None:
    __latest_tweet_id__ = latest_tweet_id_file_read(latest_tweet_file)
    bot = telegram.ext.ExtBot(TelegramTOKEN)
    # Bot close will throw RetryAfter error if from start was < 10 minutes, will throw BadRequest after few days running
    # so we always try to close and reopen it
    closed = False
    while True:
        sleep(60)
        logger.debug("Started newsletter job")
        tweet = twitter_fetch(twitter)
        if tweet:
            # restart bot if closed
            if closed:
                bot = telegram.ext.ExtBot(TelegramTOKEN)
            # fetched tweet wasn't sent earlier - update tweetid in file and send it
            if not __latest_tweet_id__ == tweet.tweet_id:
                __latest_tweet_id__ = tweet.tweet_id
                write_latest_tweet(latest_tweet_file, tweet)
                logger.info("Updated last tweet id in file")
            # inline button
            inline_url = telegram.InlineKeyboardMarkup([[
                telegram.InlineKeyboardButton(text='Link to source',
                                              url=tweet.tweet_url)
            ]])
            if subscriptions:
                for user in subscriptions:
                    try:
                        if tweet.is_media:
                            bot.sendPhoto(
                                chat_id=user, photo=tweet.media, caption=tweet.string(),
                                reply_markup=inline_url, parse_mode=telegram.ParseMode.HTML
                            )
                        else:
                            bot.sendMessage(
                                chat_id=user, text=tweet.string(), reply_markup=inline_url,
                                parse_mode=telegram.ParseMode.HTML
                            )
                    # will skip sending to current user if RetryAfter, but will wait before sending to next one
                    except telegram.error.RetryAfter as t:
                        logger.debug(f"RetryAfter error when sending photo: {t}")
                        sleep(t.retry_after)
                    # can't do anything here except skip this user
                    except telegram.error.BadRequest as b:
                        logger.error(f"Bad request when sending photo: {b}")
                        continue
                    except telegram.error.Unauthorized:
                        subscriptions.remove(user)
                        logger.warning(f"{user} blocked the bot without unsubscribing first. Removed from list.")
            logger.info(f'Send newsletter to {len(subscriptions)} users')
        # try to close bot
        elif not closed:
            try:
                bot.close()
                closed = True
            except telegram.error.TelegramError:
                closed = False
                logger.debug("Error closing the bot")
        logger.debug("Ended newsletter job")


# Twitter wrapper
def twitter_fetch(_twitter: tweepy.api) -> Union[ParsedTweetToTelegram, None]:
    logger.debug("Running twitter_fetch")
    _tweets = _twitter.user_timeline(id=elonID,
                                     # maximum tweets (works badly if only next tweet in case retweets were created)
                                     count=200,
                                     # exclude retweets
                                     include_rts=False,
                                     # exclude replies
                                     exclude_replies=True,
                                     # entities list for media
                                     include_entities=True,
                                     # to get full text
                                     tweet_mode='extended',
                                     # starting from (now old) newest tweet
                                     since_id=latest_tweet_id_file_read(latest_tweet_file)
                                     )
    if _tweets:
        logger.info(f"Got new tweet from Elon with id {_tweets[-1].id}")
        return ParsedTweetToTelegram(_tweets[-1])
    else:
        return None


def get_latest(_twitter: tweepy.api) -> Union[ParsedTweetToTelegram, None]:
    latest_ = None
    t_id = latest_tweet_id_file_read(latest_tweet_file)
    try:
        latest_ = ParsedTweetToTelegram(
            _twitter.get_status(
                # entities list for media
                include_entities=True,
                # to get full text
                tweet_mode='extended',
                # starting from newest tweet
                id=t_id
            )
        )
    except tweepy.error.TweepError:
        logger.exception(f"Error getting tweet with ID {t_id}: {tweepy.error.TweepError}")
    return latest_


# Telegram commands

# Get username or full name from message obj
def username(update: telegram.Update) -> str:
    user = update.message.from_user
    if user.username:
        return str(f'@{user.username}')
    elif user.full_name:
        return str(user.full_name)
    else:
        return str('User')


# /start is used when writing to the bot first time
def start(update: telegram.Update, context: telegram.ext.CallbackContext) -> None:
    userid = update.message.from_user.id
    if userid not in subscriptions:
        start_string = 'To subscribe - use /subscribe command.'
    else:
        start_string = 'To unsubscribe - use /unsubscribe command.'
    update.message.reply_html(
        f'Greetings {username(update)},\n'
        f'I am a bot that parses Elon Musk\'s tweets!\n'
        f'{start_string}\n'
        f'To view more info about me - use /help command.\n',
        reply_markup=telegram.ReplyKeyboardRemove()
    )


def subscribe(update: telegram.Update, context: telegram.ext.CallbackContext) -> None:
    userid = update.message.from_user.id
    if userid not in subscriptions:
        subscriptions.append(userid)
        update.message.reply_html('You have subscribed for newsletter!')
        logger.info(f"{str(userid)}:{username(update)} subscribed")
    else:
        update.message.reply_html('You are already subscribed!')


def unsubscribe(update: telegram.Update, context: telegram.ext.CallbackContext) -> None:
    userid = update.message.from_user.id
    if userid in subscriptions:
        subscriptions.remove(userid)
        update.message.reply_html(fr'You have unsubscribed from newsletter!')
        logger.info(f"{str(userid)}:{username(update)} unsubscribed")
    else:
        update.message.reply_html('You are already unsubscribed!')


# /latest to send last tweet. Will send an error when tweet is no longer available
# TODO: add fallback - previous-previous tweet
def latest(update: telegram.Update, context: telegram.ext.CallbackContext) -> None:
    # get the tweet by ID and use parser class
    latest_tweet = get_latest(twitter)
    if not latest_tweet == None:
        # declare inline keyboard for "link to tweet" button
        inline_url = telegram.InlineKeyboardMarkup([[
            telegram.InlineKeyboardButton(text='Link to source',
                                          url=latest_tweet.tweet_url)
        ]])
        # if tweet has media - send the media as photo with tweet text as caption
        if latest_tweet.is_media:
            update.message.reply_photo(
                latest_tweet.media,
                caption=latest_tweet.string(),
                reply_markup=inline_url
            )
        # if tweet doesn't have any media - just send the text
        else:
            # Example of usage of inline keyboard (i.e. keyboard right under respective message)
            update.message.reply_html(
                latest_tweet.string(),
                reply_markup=inline_url
            )
    else:
        update.message.reply_html(
            f"Sorry, \nI've encountered an error getting latest tweet, \nprobably it was deleted. \nTry again later"
        )


def status(update: telegram.Update, context: telegram.ext.CallbackContext) -> None:
    userid = update.message.from_user.id
    if userid in subscriptions:
        update.message.reply_html('You are subscribed')
    else:
        update.message.reply_html('You are not subscribed')


def help_me(update: telegram.Update, context: telegram.ext.CallbackContext) -> None:
    update.message.reply_html(
        'This bot is created to send you notifications with Elon Musk\'s latest tweets.\n'
        f'To subscribe - use /subscribe command.\n'
        f'To unsubscribe - use /unsubscribe command.\n'
        f'To receive latest Elon Musk\'s tweet - use /latest command.\n'
        f'To view this info - click or text me /help.\n'
        '\n'
        'Creator of this bot: @CamperN00b'
    )


# global variables
subscriptions = subscribers_file_read(subscribers_file)
logger = logging.getLogger(__name__)
twitter = tweepy.API(twitter_auth)

# config logger
logger_config(logger, is_debug=False)


@recursive_handler
def main():
    telegram_updater = telegram.ext.Updater(TelegramTOKEN)
    telegram_dispatcher = telegram_updater.dispatcher
    # Define commands for bot
    telegram_dispatcher.add_handler(telegram.ext.CommandHandler("start", start))
    telegram_dispatcher.add_handler(telegram.ext.CommandHandler("subscribe", subscribe))
    telegram_dispatcher.add_handler(telegram.ext.CommandHandler("unsubscribe", unsubscribe))
    telegram_dispatcher.add_handler(telegram.ext.CommandHandler("latest", latest))
    telegram_dispatcher.add_handler(telegram.ext.CommandHandler("status", status))
    telegram_dispatcher.add_handler(telegram.ext.CommandHandler("help", help_me))
    # Connect to Telegram, wait 2 seconds for updates, disconnect, wait 10 seconds before next connection
    telegram_updater.start_polling(poll_interval=2, timeout=10)
    # Blocks the thread. Sends termination signals, but I don't use them here
    telegram_updater.idle()


# Create new threads, give them a name which logger will use, and target method to run
update_subscribers_file = threading.Thread(name='update_subscribers_file', target=update_subscribers)
news_letter = threading.Thread(name='news_letter', target=newsletter)
# Start created threads
update_subscribers_file.start()
news_letter.start()

# Run main method
main()
