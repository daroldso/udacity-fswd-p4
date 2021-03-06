# -*- coding: utf-8 -*-`
"""api.py - Create and configure the Game API exposing the resources.
This can also contain game logic. For more complex games it would be wise to
move game logic to another file. Ideally the API will be simple, concerned
primarily with communication to/from the API's users."""


import logging
import endpoints
from protorpc import remote, messages
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import User, Game, Score
from models import StringMessage, NewGameForm, GameForm, MakeMoveForm,\
    ScoreForms, GameForms, RankForm, RankForms, GameStepForm, GameStepForms,\
    GridForm
from utils import get_by_urlsafe
from game import GameLogic
from datetime import datetime

import operator

NEW_GAME_REQUEST = endpoints.ResourceContainer(NewGameForm)
GET_GAME_REQUEST = endpoints.ResourceContainer(
    urlsafe_game_key=messages.StringField(1),)
MAKE_MOVE_REQUEST = endpoints.ResourceContainer(
    MakeMoveForm,
    urlsafe_game_key=messages.StringField(1),)
USER_REQUEST = endpoints.ResourceContainer(user_name=messages.StringField(1),
                                           email=messages.StringField(2))
GET_HIGHSCORE_REQUEST = endpoints.ResourceContainer(
    number_of_results=messages.IntegerField(1),)


@endpoints.api(name='battleship', version='v1')
class BattleshipApi(remote.Service):

    """Game API"""
    @endpoints.method(request_message=USER_REQUEST,
                      response_message=StringMessage,
                      path='user',
                      name='create_user',
                      http_method='POST')
    def create_user(self, request):
        """Create a User. Requires a unique username"""
        if User.query(User.name == request.user_name).get():
            raise endpoints.ConflictException(
                'A User with that name already exists!')
        user = User(name=request.user_name, email=request.email)
        user.put()
        return StringMessage(message='User {} created!'.format(
            request.user_name))

    @endpoints.method(request_message=NEW_GAME_REQUEST,
                      response_message=GameForm,
                      path='game',
                      name='new_game',
                      http_method='POST')
    def new_game(self, request):
        """Creates new game"""
        user1 = User.query(User.name == request.player1_name).get()
        if not user1:
            raise endpoints.NotFoundException(
                'User 1 does not exist!')
        user2 = User.query(User.name == request.player2_name).get()
        if not user2:
            user2key = user2
        else:
            user2key = user2.key

        player1_primary_grid = GameLogic.place_ship_on_grid(
                                request,
                                '1')
        player2_primary_grid = GameLogic.place_ship_on_grid(
                                request,
                                '2')
        player1_tracking_grid = GameLogic.create_default_grid()
        player2_tracking_grid = GameLogic.create_default_grid()

        game = Game.new_game(user1.key, user2key,
                             player1_primary_grid,
                             player2_primary_grid,
                             player1_tracking_grid,
                             player2_tracking_grid)

        return game.to_form('Good luck playing Battleship!')

    @endpoints.method(request_message=GET_GAME_REQUEST,
                      response_message=GameForm,
                      path='game/{urlsafe_game_key}',
                      name='get_game',
                      http_method='GET')
    def get_game(self, request):
        """Return the current game state."""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game:
            if game.game_over:
                return game.to_game_over_form('Game already over!')
            else:
                return game.to_form('Time to make a move!')
        else:
            raise endpoints.NotFoundException('Game not found!')

    @endpoints.method(request_message=MAKE_MOVE_REQUEST,
                      response_message=GameForm,
                      path='game/{urlsafe_game_key}',
                      name='make_move',
                      http_method='PUT')
    def make_move(self, request):
        """Makes a move. Returns a game state with message"""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game.game_over:
            return game.to_game_over_form('Game already over!')

        if game.cancelled:
            return game.to_game_over_form('Game already cancelled!')

        # Check if this move is from the correct player
        if GameLogic.is_correct_player(game, request.is_player1_move) is False:
            return game.to_game_move_form('It is not your turn!',
                                          request.is_player1_move)

        player_move, is_ship_hit, ship_being_hit, is_ship_destroyed = \
            GameLogic.make_move(request, game)

        # Decrease the ships remaining if the ship is hit
        GameLogic.set_new_ships_remaining(
            game, is_ship_destroyed, request.is_player1_move)

        if(request.is_player1_move):
            current_player = game.player1
            next_player = game.player2
        else:
            current_player = game.player2
            next_player = game.player1

        if current_player is not None:
            current_player_name = current_player.get().name
        else:
            current_player_name = 'Computer'

        if next_player is not None:
            next_player_name = next_player.get().name
        else:
            next_player_name = 'Computer'

        msg = '%s has hit the ' % current_player_name
        if is_ship_hit:
            msg += '%s of %s' % (ship_being_hit, next_player_name)
        else:
            msg += 'nothing'

        if is_ship_destroyed:
            msg += ' and sunk it'

        # Save move to game history
        move = GameStepForm()
        move.player = current_player_name
        move.move = player_move
        move.is_ship_destroyed = is_ship_destroyed
        game.history.append(move)

        # Update last move time
        game.last_move = datetime.now()

        if game.player1_ships_remaining < 1 or \
           game.player2_ships_remaining < 1:
            if game.player1_ships_remaining < 1:
                game.end_game(game.player2)
                if game.player2 is not None:
                    winner_name = game.player2.get().name
                else:
                    winner_name = 'Computer'
            else:
                game.end_game(game.player1)
                if game.player1 is not None:
                    winner_name = game.player1.get().name
                else:
                    winner_name = 'Computer'
            return game.to_game_over_form('Game over! %s wins!' % winner_name)
        else:
            game.current_player = next_player
            game.put()
            # Send a reminder email to opponent upon each move
            if game.current_player is not None:
                taskqueue.add(
                    url='/tasks/send_notification_to_opponent',
                    params={'player_to_move': next_player_name})
            return game.to_game_move_form('%s! %s\'s turn' % (
                                          msg,
                                          next_player_name
                                          ), request.is_player1_move)

    @endpoints.method(response_message=ScoreForms,
                      path='scores',
                      name='get_scores',
                      http_method='GET')
    def get_scores(self, request):
        """Return all scores"""
        return ScoreForms(items=[score.to_form() for score in Score.query()])

    @endpoints.method(request_message=USER_REQUEST,
                      response_message=ScoreForms,
                      path='scores/user/{user_name}',
                      name='get_user_scores',
                      http_method='GET')
    def get_user_scores(self, request):
        """Returns all of an individual User's scores"""
        user = User.query(User.name == request.user_name).get()
        if not user:
            raise endpoints.NotFoundException(
                'A User with that name does not exist!')
        scores = Score.query(Score.winner == user.key)
        return ScoreForms(items=[score.to_form() for score in scores])

    @endpoints.method(request_message=USER_REQUEST,
                      response_message=GameForms,
                      path='game/user/{user_name}',
                      name='get_user_games',
                      http_method='GET')
    def get_user_games(self, request):
        """Returns all of a User's active games"""
        user = User.query(User.name == request.user_name).get()
        if not user:
            raise endpoints.NotFoundException(
                'A User with that name does not exist!')
        games = Game.query(ndb.AND(Game.game_over == False,
                                   Game.cancelled == False,
                                   ndb.OR(Game.player1 == user.key,
                                          Game.player2 == user.key)))
        return GameForms(items=[game.to_form('') for game in games])

    @endpoints.method(request_message=GET_GAME_REQUEST,
                      response_message=GameForm,
                      path='game',
                      name='cancel_game',
                      http_method='PUT')
    def cancel_game(self, request):
        """Cancel the game and return the current game state."""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game:
            if game.game_over:
                return game.to_game_over_form('Game already over!')
            elif game.cancelled:
                return game.to_game_over_form('Game already cancelled!')
            else:
                game.cancelled = True
                game.put()
                return game.to_game_over_form('Game Cancelled!')
        else:
            raise endpoints.NotFoundException('Game not found!')

    @endpoints.method(request_message=GET_HIGHSCORE_REQUEST,
                      response_message=ScoreForms,
                      path='scores/highscore',
                      name='get_high_scores',
                      http_method='GET')
    def get_high_scores(self, request):
        """Return all scores sorted by their ships remaining"""
        scores = Score.query()
        scores = scores.order(-Score.ships_remaining)
        try:
            scores = scores.fetch(request.number_of_results)
        except:
            raise endpoints.BadRequestException(
                'Please put in a positive number')
        return ScoreForms(items=[score.to_form() for score in scores])

    @endpoints.method(response_message=RankForms,
                      path='scores/ranking',
                      name='get_user_rankings',
                      http_method='GET')
    def get_user_rankings(self, request):
        """Returns the ranking of users"""
        scores = Score.query()
        scores_grouped_by_user = {}

        for score in scores:
            name = score.winner.get().name
            if name in scores_grouped_by_user:
                scores_grouped_by_user[name] += score.ships_remaining
            else:
                scores_grouped_by_user[name] = score.ships_remaining

        rankings = sorted(
            scores_grouped_by_user.items(), key=operator.itemgetter(1))
        rankings.reverse()

        def make_rank_form(score):
            form = RankForm()
            form.user = score[0]
            form.score = score[1]
            return form

        return RankForms(items=[make_rank_form(score) for score in rankings])

    @endpoints.method(request_message=GET_GAME_REQUEST,
                      response_message=GameStepForms,
                      path='game/{urlsafe_game_key}/history',
                      name='get_game_history',
                      http_method='GET')
    def get_game_history(self, request):
        """Return the history of game in an array of moves"""
        game = get_by_urlsafe(request.urlsafe_game_key, Game)
        if game:
            return GameStepForms(items=[GameStepForm(
                                 player=step.player,
                                 move=step.move,
                                 is_ship_destroyed=step.is_ship_destroyed)
                                 for step in game.history])
        else:
            raise endpoints.NotFoundException('Game not found!')

    @staticmethod
    def _get_dormant_games():
        """Return the games with last move time later than 12 hours"""
        dormant_games = []
        games = Game.query(ndb.AND(Game.game_over == False,
                                   Game.cancelled == False))
        for game in games:
            elapsedTime = datetime.now() - game.last_move
            elaspedHours, elaspedSeconds = divmod(
                elapsedTime.days * 86400 + elapsedTime.seconds, 3600)
            if elaspedHours >= 12:
                dormant_games.append(game)

        return dormant_games

api = endpoints.api_server([BattleshipApi])
