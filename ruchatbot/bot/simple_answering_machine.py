# -*- coding: utf-8 -*-

import json
import os
import math
import logging
import numpy as np
import operator
import random
import requests

from ruchatbot.bot.base_answering_machine import BaseAnsweringMachine
from ruchatbot.bot.simple_dialog_session_factory import SimpleDialogSessionFactory
# from xgb_relevancy_detector import XGB_RelevancyDetector
from ruchatbot.bot.lgb_relevancy_detector import LGB_RelevancyDetector
#from nn_relevancy_tripleloss import NN_RelevancyTripleLoss
#from xgb_person_classifier_model import XGB_PersonClassifierModel
#from nn_person_change import NN_PersonChange
from ruchatbot.bot.answer_builder import AnswerBuilder
from ruchatbot.bot.interpreted_phrase import InterpretedPhrase
from ruchatbot.bot.nn_enough_premises_model import NN_EnoughPremisesModel
# from nn_synonymy_detector import NN_SynonymyDetector
from ruchatbot.bot.lgb_synonymy_detector import LGB_SynonymyDetector
# from nn_synonymy_tripleloss import NN_SynonymyTripleLoss
from ruchatbot.bot.jaccard_synonymy_detector import Jaccard_SynonymyDetector

#from ruchatbot.bot.nn_interpreter import NN_Interpreter
#from ruchatbot.bot.nn_interpreter_new2 import NN_InterpreterNew2
from ruchatbot.bot.nn_interpreter6 import NN_InterpreterNew6

from ruchatbot.bot.lgb_req_interpretation import LGB_ReqInterpretation
from ruchatbot.bot.modality_detector import ModalityDetector
from ruchatbot.bot.simple_modality_detector import SimpleModalityDetectorRU
from ruchatbot.bot.no_information_model import NoInformationModel
from ruchatbot.bot.intent_detector import IntentDetector
from ruchatbot.generative_grammar.generative_grammar_engine import GenerativeGrammarEngine
from ruchatbot.bot.entity_extractor import EntityExtractor
from ruchatbot.bot.running_form_status import RunningFormStatus
from ruchatbot.bot.running_scenario import RunningScenario
from ruchatbot.bot.p2q_relevancy_lgb import P2Q_Relevancy_LGB
from ruchatbot.bot.paraphraser import Paraphraser
from ruchatbot.bot.actors import substitute_bound_variables, SayingPhrase
from ruchatbot.bot.discourse import Discourse


class InsteadofRuleResult(object):
    def __init__(self):
        self.insteadof_applied = None
        self.other_applied = None
        self.replica_is_generated = None

    @staticmethod
    def GetTrueInsteadof(replica_is_generated):
        res = InsteadofRuleResult()
        res.insteadof_applied = True
        res.other_applied = False
        res.replica_is_generated = replica_is_generated
        return res

    @staticmethod
    def GetTrueOther(replica_is_generated):
        res = InsteadofRuleResult()
        res.insteadof_applied = False
        res.other_applied = True
        res.replica_is_generated = replica_is_generated
        return res

    @staticmethod
    def GetFalse():
        res = InsteadofRuleResult()
        res.insteadof_applied = False
        res.other_applied = False
        return res

    def is_any_applied(self):
        return self.insteadof_applied or self.other_applied


def same_stem2(word, key_stems):
    for stem in key_stems:
        if stem in word:
            return True
    return False


class SimpleAnsweringMachine(BaseAnsweringMachine):
    """
    Движок чатбота на основе набора нейросетевых и прочих моделей (https://github.com/Koziev/chatbot).
    Методы класса реализуют workflow обработки реплик пользователя - формирование ответов, управление
    базой знаний.
    """

    def __init__(self, text_utils):
        super(SimpleAnsweringMachine, self).__init__()
        self.trace_enabled = False
        self.session_factory = SimpleDialogSessionFactory()
        self.text_utils = text_utils
        self.logger = logging.getLogger('SimpleAnsweringMachine')
        self.discourse = Discourse()

        self.chitchat_base_url = None  #'http://127.0.0.1:9099/reply?context={}'

        self.premise_not_found = None  # модель генерации реплик для вопросов, на которые бот не знает ответ
        self.premise_not_found_count = 0  # сколько раз вызывалась модель premise_not_found

        # Если релевантность факта к вопросу в БФ ниже этого порога, то факт не подойдет
        # для генерации ответа на основе факта.
        self.min_premise_relevancy = 0.6
        self.min_faq_relevancy = 0.7

    def get_text_utils(self):
        return self.text_utils

    def get_model_filepath(self, models_folder, old_filepath):
        """
        Для внутреннего использования - корректирует абсолютный путь
        к файлам данных модели так, чтобы был указанный каталог.
        """
        _, tail = os.path.split(old_filepath)
        return os.path.join(models_folder, tail)

    def load_models(self, data_folder, models_folder, constants):
        self.logger.info(u'Loading models from "%s"', models_folder)
        self.models_folder = models_folder

        self.premise_not_found = NoInformationModel()
        self.premise_not_found.load(models_folder, data_folder, constants, self.text_utils)

        # Загружаем общие параметры для сеточных моделей
        with open(os.path.join(models_folder, 'qa_model_selector.config'), 'r') as f:
            model_config = json.load(f)
            self.max_inputseq_len = model_config['max_inputseq_len']
            self.wordchar2vector_path = self.get_model_filepath(models_folder, model_config['wordchar2vector_path'])
            self.PAD_WORD = model_config['PAD_WORD']
            self.word_dims = model_config['word_dims']

        self.qa_model_config = model_config

        # TODO: выбор конкретной реализации для каждого типа моделей сделать внутри базового класса
        # через анализ поля 'engine' в конфигурации модели. Для нейросетевых моделей там будет
        # значение 'nn', для градиентного бустинга - 'xgb'. Таким образом, уберем ненужную связность
        # данного класса и конкретных реализации моделей.

        # Определение релевантности предпосылки и вопроса на основе XGB модели
        # self.relevancy_detector = XGB_RelevancyDetector()
        self.relevancy_detector = LGB_RelevancyDetector()
        # self.relevancy_detector = NN_RelevancyTripleLoss()
        self.relevancy_detector.load(models_folder)

        # Модель определения синонимичности двух фраз
        # self.synonymy_detector = NN_SynonymyDetector()
        # self.synonymy_detector = NN_SynonymyTripleLoss()
        self.synonymy_detector = LGB_SynonymyDetector()
        self.synonymy_detector.load(models_folder)
        # self.synonymy_detector = Jaccard_SynonymyDetector()

        # Интерпретатор для раскрытия анафоры, заполнения гэппинга, эллипсиса и т.д.
        #self.interpreter = NN_InterpreterNew2()
        self.interpreter = NN_InterpreterNew6()
        self.interpreter.load(models_folder)

        #self.req_interpretation = NN_ReqInterpretation()
        self.req_interpretation = LGB_ReqInterpretation()
        self.req_interpretation.load(models_folder)

        self.p2q_relevancy = P2Q_Relevancy_LGB()
        self.p2q_relevancy.load(models_folder)

        # Определение достаточности набора предпосылок для ответа на вопрос
        self.enough_premises = NN_EnoughPremisesModel()
        self.enough_premises.load(models_folder)

        # Комплексная модель (группа моделей) для генерации текста ответа
        self.answer_builder = AnswerBuilder()
        self.answer_builder.load_models(models_folder, self.text_utils)

        # Генеративная грамматика для формирования реплик
        self.replica_grammar = None
        #self.replica_grammar = GenerativeGrammarEngine()
        #with open(os.path.join(models_folder, 'replica_generator_grammar.bin'), 'rb') as f:
        #    self.replica_grammar = GenerativeGrammarEngine.unpickle_from(f)
        #self.replica_grammar.set_dictionaries(self.text_utils.gg_dictionaries)

        # Классификатор грамматического лица на базе XGB
        #self.person_classifier = XGB_PersonClassifierModel()
        #self.person_classifier.load(models_folder)

        # Нейросетевая модель для манипуляции с грамматическим лицом
        #self.person_changer = NN_PersonChange()
        #self.person_changer.load(models_folder)

        # Модель определения модальности фраз собеседника
        self.modality_model = SimpleModalityDetectorRU()
        self.modality_model.load(models_folder)

        self.intent_detector = IntentDetector()
        self.intent_detector.load(models_folder)

        self.entity_extractor = EntityExtractor()
        self.entity_extractor.load(models_folder)

        self.jsyndet = Jaccard_SynonymyDetector()

        self.paraphraser = Paraphraser()
        self.paraphraser.load(models_folder)

        self.logger.debug('All models loaded')

    def extract_entity(self, entity_name, phrase_str):
        return self.entity_extractor.extract_entity(entity_name, phrase_str, self.text_utils, self.text_utils.word_embeddings)

    def start_conversation(self, bot, interlocutor):
        """
        Начало общения бота с interlocutor. Ни одной реплики еще не было.
        Бот может поприветствовать собеседника или напомнить ему что-то, если
        в сессии с ним была какая-то напоминалка, т.д. Фразу, которую надо показать собеседнику,
        поместим в буфер выходных фраз с помощью метода say, а внешний цикл обработки уже извлечет ее оттуда
        и напечатает в консоли и т.д.

        :param bot: экземпляр класса BotPersonality
        :param interlocutor: строковый идентификатор собеседника.
        :return: строка реплики, которую скажет бот.
        """
        session = self.get_session(bot, interlocutor)
        if bot.has_scripting():
            phrase = bot.scripting.start_conversation(self, session)
            if phrase is not None:
                self.say(bot, session, phrase)

    def get_session_factory(self):
        return self.session_factory

    def is_question(self, phrase):
        modality, person = self.modality_model.get_modality(phrase, self.text_utils, self.word_embeddings)
        return modality == ModalityDetector.question

    def translate_interlocutor_replica(self, bot, session, raw_phrase):
        rules = bot.get_comprehension_templates().get_templates()
        order2anchor = dict((order, anchor) for (anchor, order) in rules)
        phrases = list(order for (anchor, order) in rules)
        phrases2 = list((self.text_utils.wordize_text(order), None, None) for (anchor, order) in rules)
        #canonized2raw = dict((f2[0], f1) for (f1, f2) in itertools.izip(phrases, phrases2))
        canonized2raw = dict((f2[0], f1) for (f1, f2) in zip(phrases, phrases2))

        raw_phrase2 = self.text_utils.wordize_text(raw_phrase)
        best_order, best_sim = self.synonymy_detector.get_most_similar(raw_phrase2,
                                                                       phrases2,
                                                                       self.text_utils,
                                                                       nb_results=1)

        # Если похожесть проверяемой реплики на любой вариант в таблице приказов выше порога,
        # то дальше будем обрабатывать нормализованную фразу вместо исходной введенной.
        #comprehension_threshold = 0.70
        comprehension_threshold = bot.get_comprehension_threshold()
        if best_sim > comprehension_threshold:
            if self.trace_enabled:
                self.logger.info('Closest comprehension phrase for "%s" is "%s" with similarity=%f above threshold=%f',
                                 raw_phrase, best_order, best_sim, comprehension_threshold)

            interpreted_order = order2anchor[canonized2raw[best_order]]
            if raw_phrase2 != interpreted_order:
                if self.trace_enabled:
                    self.logger.info(u'Phrase "{}" is interpreted as "{}"'.format(raw_phrase, interpreted_order))
                return interpreted_order
            else:
                return None
        else:
            return None

    def select_relevant_replica(self, replicas, session, interlocutor):
        # TODO: использовать модель оценки уместности для взвешивания реплик
        if replicas:
            if len(replicas) == 1:
                return replicas[0].get_str()
            else:
                return sorted(replicas, key=lambda z: -z.get_rank())[0].get_str()
        else:
            return None

    def interpret_phrase0(self, bot, session, raw_phrase, expanded_phrase, internal_issuer):
        interpreted = InterpretedPhrase(raw_phrase)
        phrase_modality, phrase_person, raw_tokens = self.modality_model.get_modality(expanded_phrase, self.text_utils)

        interpreted.raw_tokens = raw_tokens

        if not internal_issuer:
            # классификация восстановленной фразы до нормализации грамматического лица
            if self.intent_detector is not None:
                interpreted.intents = self.intent_detector.detect_intent(expanded_phrase, self.text_utils)
                self.logger.debug('raw_phrase="%s" expanded_phrase="%s" intents="%s"', raw_phrase,
                                  expanded_phrase,
                                  ','.join(interpreted.intents))

            # Попробуем найти шаблон трансляции, достаточно похожий на эту фразу.
            # Может получиться так, что введенная императивная фраза станет обычным вопросом:
            # "назови свое имя!" ==> "Как тебя зовут?"
            translated_str = self.translate_interlocutor_replica(bot, session, expanded_phrase)
            if translated_str is not None:
                expanded_phrase = translated_str
                phrase_modality, phrase_person, _ = self.modality_model.get_modality(expanded_phrase, self.text_utils)
                if phrase_person == 2:
                    phrase_person = 1
            else:
                expanded_phrase = self.interpreter.normalize_person(expanded_phrase, self.text_utils)
        else:
            interpreted.intents = []

        interpreted.interpretation = expanded_phrase

        interpreted.set_modality(phrase_modality, phrase_person)

        # Для работы некоторых типов правил нужна грамматическая информация
        words = self.text_utils.tokenize(expanded_phrase)
        interpreted.tags = list(self.text_utils.tag(words, with_lemmas=True))

        return interpreted

    def interpret_phrase(self, bot, session, raw_phrase, internal_issuer):
        """
        ИНТЕРПРЕТАЦИЯ РЕПЛИКИ
        В ходе интерпретации раскрывается анаформа, заполняется гэппинг, эллипсис, разбивка
        на фактические фразы с учетом восстановления лексического содержания, также в некоторых
        случаях происходит нормализация текста реплики.
        """

        if internal_issuer:
            # Считаем, что внутренние вопросы (задаваемые в рамках механизма рефлексии, чтобы
            # определить наличие информации в базе данных) не нуждаются в раскрытии в контексте.
            return [self.interpret_phrase0(bot, session, raw_phrase, raw_phrase, internal_issuer)]

        interpretations = []

        # Так модель интерпретатора дает еще много ошибок, помогаем ей - определяем необходимость
        # выполнять интерпретацию текущей фразы, используя бинарный классификатор.
        if self.req_interpretation.require_interpretation(raw_phrase, self.text_utils):
            # Готовим контекст для интерпретации - две предыдущие фразы
            context_phrases = list()

            if len(session.conversation_history) > 0:
                last_phrase = session.conversation_history[-1]

                if len(session.conversation_history) > 1:
                    # Если предыдущая фраза тоже нуждалась в интерпретации, то добавим в контекст еще одну фразу перед ней.
                    if self.req_interpretation.require_interpretation(last_phrase.raw_phrase, self.text_utils):
                        last2_phrase = session.conversation_history[-2]  # это вопрос человека "Ты яблоки любишь?"
                        context_phrases.append(last2_phrase.raw_phrase)

                # Добавляем в контекст предшествующую фразу.
                context_phrases.append(last_phrase.raw_phrase)

            context_phrases.append(raw_phrase)  # это интерпретируемая реплика
            expansion = self.interpreter.interpret(context_phrases, self.text_utils)

            # 04-11-2020 проверим, не вернул ли интерпретатор абракадабру
            if self.intent_detector.detect_abracadabra(expansion, self.text_utils):
                self.logger.debug('Interpreter returned abracadabra "%s" for context "%s"', expansion, ' | '.join(context_phrases) )
                # оставим исходную фразу - это лучше, чем мусор.
                expansion = raw_phrase

            # После интерпретации фраза может содержать несколько клауз, разделенных точкой.
            expansion2 = expansion.replace('.', '.|').replace('?', '?|')
            clauses = [s.strip() for s in expansion2.split('|') if sum(c in 'абвгдеёжзийклмпнопрстуфхцчшщъыьэюя23456789abcdefghijklmnopqrstuvwxyz' for c in s) > 0]
            for i, clause in enumerate(clauses):
                raw_phrase2 = raw_phrase if i == 0 else ''
                interpreted = self.interpret_phrase0(bot, session, raw_phrase2, clause, internal_issuer)
                interpretations.append(interpreted)
        else:
            interpreted = self.interpret_phrase0(bot, session, raw_phrase, raw_phrase, internal_issuer)
            interpretations.append(interpreted)

        return interpretations

    def say(self, bot, session, answer):
        self.logger.info('Say "%s"', answer)
        if answer:
            new_is_question = answer.endswith('?')
            new_is_assertion = not answer.endswith('?')

            last_phrase = session.get_last_utterance()
            last_is_question = last_phrase and last_phrase.is_bot_phrase and last_phrase.raw_phrase.endswith('?')
            last_is_assertion = last_phrase and last_phrase.is_bot_phrase and not last_phrase.raw_phrase.endswith('?')

            # НАЧАЛО ОТЛАДКИ
            #self.logger.debug('DEBUG@382 last_phrase=%s', last_phrase)
            # КОНЕЦ ОТЛАДКИ

            if new_is_question and last_is_question:
                # Два вопроса подряд - плохо.
                self.logger.error('Two consequent questions issued by bot: prev="%s", new="%s"', last_phrase.raw_phrase, answer)
                return

            if new_is_assertion and last_is_question:
                # Утверждение после вопроса - плохо, так как вопрос экранируется
                self.logger.error('Assertion after the question issued by bot: prev="%s", new="%s"', last_phrase.raw_phrase, answer)
                return

            # Более 2х реплик подряд от бота - слишком много.
            nb = session.count_prev_consequent_b()

            # НАЧАЛО ОТЛАДКИ
            #self.logger.debug('DEBUG@400 nb=%d', nb)
            # КОНЕЦ ОТЛАДКИ

            if nb >= 2:
                self.logger.error('More than 2 utterances issued by bot: prev="%s", new="%s"', last_phrase.raw_phrase, answer)
                return

            answer = self.paraphraser.paraphrase(answer, self.text_utils)
            answer_interpretation = InterpretedPhrase(answer)
            answer_interpretation.is_bot_phrase = True
            phrase_modality, phrase_person, raw_tokens = self.modality_model.get_modality(answer, self.text_utils)
            answer_interpretation.set_modality(phrase_modality, phrase_person)
            session.add_to_buffer(answer)
            session.add_phrase_to_history(answer_interpretation)
            self.discourse.process_bot_phrase(bot, session, answer)
        else:
            self.logger.error('Empty phrase in say()')

    def say_before_b(self, bot, session, answer):
        self.logger.info('Say before B: "%s"', answer)
        new_is_question = answer.endswith('?')
        new_is_assertion = not answer.endswith('?')

        last_phrase = session.get_last_utterance()
        last_is_question = last_phrase and last_phrase.is_bot_phrase and last_phrase.raw_phrase.endswith('?')
        last_is_assertion = last_phrase and last_phrase.is_bot_phrase and not last_phrase.raw_phrase.endswith('?')

        if new_is_question and last_is_question:
            # Два вопроса подряд - плохо.
            self.logger.error('Two consequent questions issued by bot: prev="%s", new="%s"', last_phrase.raw_phrase, answer)
            return

        if new_is_assertion and last_is_question:
            # Утверждение после вопроса - плохо, так как вопрос экранируется
            self.logger.error('Assertion after the question issued by bot: prev="%s", new="%s"', last_phrase.raw_phrase, answer)
            return

        # Более 2х реплик подряд от бота - слишком много.
        nb = session.count_prev_consequent_b()

        if nb >= 2:
            self.logger.error('More than 2 utterances issued by bot: prev="%s", new="%s"', last_phrase.raw_phrase, answer)
            return

        answer = self.paraphraser.paraphrase(answer, self.text_utils)
        answer_interpretation = InterpretedPhrase(answer)
        answer_interpretation.is_bot_phrase = True
        phrase_modality, phrase_person, raw_tokens = self.modality_model.get_modality(answer, self.text_utils)
        answer_interpretation.set_modality(phrase_modality, phrase_person)
        session.insert_into_buffer(answer)
        session.add_phrase_to_history(answer_interpretation)
        self.discourse.process_bot_phrase(bot, session, answer)

    def run_scenario(self, scenario, bot, session, interlocutor, interpreted_phrase):
        """Замещающий запуск сценария: если текущий сценарий имеет более низкий приоритет, то он
        будет полностью прекращен."""
        if session.get_status():
            if scenario.get_name() == session.get_status().get_name():
                # Новый сценарий - такой же, как уже запущенный (например, снова сработало
                # тематическое правило, запускающие этот сценарий).
                self.logger.warning('Could not re-start dialogue "%s"', scenario.get_name())
                return
            elif scenario.get_priority() < session.get_status().get_priority():
                # Текущий сценарий имеет приоритет выше, чем новый. Поэтому новый пока откладываем.
                self.logger.warning(u'New status priority %d is lower than priority %d of running "%s"', scenario.get_priority(), session.get_status().get_priority(), session.get_status().get_name())
                session.defer_status(scenario)
                return
            elif scenario.get_priority() == session.get_status().get_priority():
                # Тут могут быть разные нюансы, которые неплохо бы регулировать попарными свойствами.
                # Но это будет слишком муторно для разработчика сценариев.
                # Поэтому считаем, что новый сценарий вытесняет текущий в этом случае.
                self.logger.debug(u'New scenario "%s" priority=%d is same as priority of currently running "%s"',
                                  scenario.get_name(), scenario.get_priority(), session.get_status().get_name())
            else:
                self.logger.debug(u'New scenario priority=%d is higher than currently running=%d',
                                  scenario.get_priority(), session.get_status().get_priority())
        else:
            self.logger.debug(u'Start scenario "%s"', scenario.name)

        status = RunningScenario(scenario, current_step_index=-1)
        session.set_status(status)
        scenario.started(status, bot, session, interlocutor, interpreted_phrase, text_utils=self.text_utils)
        self.run_scenario_step(bot, session, interlocutor, interpreted_phrase)

    def call_scenario(self, scenario, bot, session, interlocutor, interpreted_phrase):
        """Запуск вложенного сценария, при этом текущий сценарий приостанавливается до окончания нового."""
        # 09-12-2020 если уже есть работающий экземпляр запускаемого сценария, то не будем запускать его снова.
        if session.get_status():
            if session.get_status().get_name() == scenario.get_name():
                self.logger.debug('Scenario "%s" is already active', scenario.get_name())
                return

        status = RunningScenario(scenario, current_step_index=-1)
        session.call_scenario(status)
        scenario.started(status, bot, session, interlocutor, interpreted_phrase, text_utils=self.text_utils)
        self.run_scenario_step(bot, session, interlocutor, interpreted_phrase)

    def exit_scenario(self, bot, session, interlocutor, interpreted_phrase):
        self.logger.debug('Exit scenario "%s"', session.get_status().get_name())
        session.exit_scenario()
        if session.get_status():
            if isinstance(session.get_status(), RunningScenario):
                self.run_scenario_step(bot, session, interlocutor, interpreted_phrase)

    def run_scenario_step(self, bot, session, interlocutor, interpreted_phrase):
        running_scenario = session.get_status()
        if not isinstance(running_scenario, RunningScenario):
            self.logger.error('Expected instance of RunningScenario, found %s', type(running_scenario).__name__)
        else:
            running_scenario.scenario.run_step(running_scenario, bot, session, interlocutor, interpreted_phrase, self.text_utils)

    def run_form(self, form, bot, session, user_id, interpreted_phrase):
        filled_fields = dict()
        empty_fields = list()

        # 1) извлечь значения полей из entities в активировавшей фразе
        for field in form.fields:
            #if False:  # ДЛЯ ОТЛАДКИ!
            if field.source == 'entity':
                entity_value = bot.extract_entity(field.from_entity, interpreted_phrase)
                if entity_value:
                    filled_fields[field.name] = entity_value
                    continue
            elif field.source == 'reflection':
                raise NotImplementedError()

            empty_fields.append(field)

        # 2) остались незаполненные поля?
        if len(empty_fields) > 0:
            # надо сменить состояние сессии на "обработка формы XXX", проверить
            # начальное заполнение полей, задать первый уточняющий вопрос...
            current_field = empty_fields[0]
            status = RunningFormStatus(form, interpreted_phrase, filled_fields, current_field)
            session.set_status(status)
            bot.say(session, current_field.question)
        else:
            # Все поля формы заполнены - запускаем итоговое действие формы
            status = RunningFormStatus(form, interpreted_phrase, filled_fields, current_field=None)
            session.set_status(status)
            self.form_ok(bot, session, user_id)

    def form_ok(self, bot, session, interlocutor):
        status = session.get_status()
        assert(isinstance(status, RunningFormStatus))
        form = status.form
        if form.ok_action:
            logging.debug(u'Выполнение действия формы "%s"', form.name)
            form.compiled_ok_action.do_action(bot, session, interlocutor, None, None, text_utils=self.text_utils)
        session.form_executed()

    def does_bot_know_answer(self, question, bot, session, interlocutor):
        """Вернет true, если бот знает ответ на вопрос question"""
        memory_phrases = list(bot.facts.enumerate_facts(interlocutor))
        best_premise, best_rel = self.relevancy_detector.get_most_relevant(question,
                                                                           memory_phrases,
                                                                           self.text_utils,
                                                                           nb_results=1)
        return best_rel >= self.min_premise_relevancy

    def find_premise(self, question, bot, session, interlocutor):
        memory_phrases = list(bot.facts.enumerate_facts(interlocutor))
        best_premise, best_rel = self.relevancy_detector.get_most_relevant(question,
                                                                           memory_phrases,
                                                                           self.text_utils,
                                                                           nb_results=1)
        return best_premise if best_rel >= self.min_premise_relevancy else None

    def find_similar_fact(self, fact_str, bot, session, interlocutor):
        memory_phrases = list(bot.facts.enumerate_facts(interlocutor))
        best_fact, best_sim = self.synonymy_detector.get_most_similar(fact_str,
                                                                      memory_phrases,
                                                                      self.text_utils,
                                                                      nb_results=1)
        if best_sim >= self.synonymy_detector.get_threshold():
            return best_fact
        else:
            return None

    def find_contradictory_fact(self, fact_str, bot, session, interlocutor):
        memory_phrases = list(bot.facts.enumerate_facts(interlocutor))
        best_premise, best_rel = self.relevancy_detector.get_most_relevant(fact_str,
                                                                           memory_phrases,
                                                                           self.text_utils,
                                                                           nb_results=1)
        if best_rel >= self.min_premise_relevancy:
            # Есть релевантный факт.
            # Попробуем сгенерировать ответ.
            premises = [[best_premise]]
            premise_rels = [best_rel]
            answers, answer_rels = self.answer_builder.build_answer_text(premises, premise_rels,
                                                                         fact_str,
                                                                         self.text_utils)
            if answers:
                answer = answers[0]
                if answer != 'да':  # !хардкод русского текста ответа это плохо - потом отрефакторить!
                    return best_premise

        return None

    def calc_discourse_relevance(self, replica, session):
        """Возвращает оценку соответствия реплики replica текущему дискурсу беседы session"""
        # TODO
        return 1.0

    def bot_replica_already_uttered(self, bot, session, phrase):
        """Проверяем, была ли такая же или синонимичная реплика уже сказана ботом ранее"""
        found_same_replica = session.count_bot_phrase(phrase) > 0
        if not found_same_replica:
            # В точности такой же реплики не было, но надо проверить на перефразировки.
            bot_phrases = [(f, None, None) for f in session.get_bot_phrases()]
            if len(bot_phrases) > 0:
                best_phrase, best_rel = self.synonymy_detector.get_most_similar(phrase, bot_phrases,
                                                                                self.text_utils,
                                                                                nb_results=1)
                if best_rel >= self.synonymy_detector.get_threshold():
                    found_same_replica = True
        return found_same_replica

    def generate_with_generative_grammar(self, bot, session, interlocutor, phrase, base_weight):
        if not bot.generative_smalltalk_enabled:
            return []

        # Используем генеративную грамматику для получения возможных реплик
        self.logger.debug('Using replica_grammar to generate replicas...')
        generated_replicas = []
        words = self.text_utils.tokenize(phrase)
        all_generated_phrases = self.replica_grammar.generate(words, self.text_utils.known_words, use_assocs=False)

        for replica in sorted(all_generated_phrases, key=lambda z: -z.get_rank())[:5]:
            replica_str = replica.get_str()
            if not self.bot_replica_already_uttered(bot, session, replica_str):
                # проверить, если replica_str является репликой-ответом: знает
                # ли бот ответ на этот вопрос.
                good_replica = True
                if replica_str[-1] == u'?':
                    if self.does_bot_know_answer(replica_str, bot, session, interlocutor):
                        good_replica = False

                if good_replica:
                    discourse_rel = self.calc_discourse_relevance(replica_str, session)
                    # TODO - добавить сюда еще взвешивание по модели синтаксической валидации
                    replica_w = discourse_rel * replica.get_rank()
                    generated_replicas.append((replica.get_str(), replica_w, 'generate_with_generative_grammar'))
                    break

        return generated_replicas

    def generate_with_common_phrases(self, bot, session, interlocutor, phrase, base_weight):
        generated_replicas = []

        # Более тяжелый алгоритм поиск подходящей реплики: ищем такую фразу, в которой
        # есть не менее одного существительного или глагола, общего с входной репликой.
        # Для этого нам надо выполнить частеречную разметку.
        phrase_words = self.text_utils.tokenize(phrase)
        phrase_tags = self.text_utils.tag(phrase_words)
        key_stems = set()
        for token in phrase_tags:
            if u'NOUN' in token[1] or u'VERB' in token[1]:
                if len(token[0]) >= 5:
                    stem = token[0][:5]
                    key_stems.add(stem)
        common_phrase_weights = []
        max_weight = 0
        for phrase2 in bot.get_common_phrases():
            words2 = self.text_utils.tokenize(phrase2)
            stem_hits = sum(same_stem2(word, key_stems) for word in words2)
            if stem_hits >= 1:
                common_phrase_weights.append((phrase2, stem_hits))
                if stem_hits > max_weight:
                    max_weight = stem_hits

        # Берем все фразы, у которых max_weight вхождений стемов.
        best_phrases = [f for (f, w) in common_phrase_weights if w == max_weight]

        # Теперь среди них найдем фразу, максимально похожую на исходную фразу
        best_phrases = [(f,) for f in best_phrases]

        if len(best_phrases) > 0:
            # TODO: возможно, вместо коэффициента Жаккара лучше использовать word mover's distance
            sim_phrases = self.jsyndet.get_most_similar(phrase, best_phrases, self.text_utils, nb_results=1)

            for f, phrase_sim in [sim_phrases]:
                if not self.bot_replica_already_uttered(bot, session, f):
                    # проверить, если f является репликой-ответом: знает
                    # ли бот ответ на этот вопрос.
                    good_replica = True
                    if f[-1] == u'?':
                        if self.does_bot_know_answer(f, bot, session, interlocutor):
                            good_replica = False

                    if good_replica:
                        discourse_rel = self.calc_discourse_relevance(f, session)
                        generated_replicas.append((f,
                                                   phrase_sim * discourse_rel * base_weight,
                                                   'generate_with_common_phrases(1)'))
                        self.logger.debug('generate_with_common_phrases input="%s" output="%s"', phrase, f)


        if False:
            # Выбираем ближайший факт
            facts0 = bot.facts.enumerate_facts(interlocutor)
            facts0 = [fact for fact in facts0 if fact[0].lower() != phrase]
            facts = []
            for fact0 in facts0:
                words2 = self.text_utils.tokenize(fact0[0])
                stem_hits = sum(same_stem2(word, key_stems) for word in words2)
                if stem_hits >= 1:
                    facts.append(fact0)

            if len(facts) > 0:
                sim_facts = self.jsyndet.get_most_similar(phrase, facts, self.text_utils, nb_results=1)
                for fact, fact_sim in [sim_facts]:
                    if fact_sim > 0.20 and fact.lower() != phrase:
                        if not self.bot_replica_already_uttered(bot, session, fact):
                            # Среди фактов не может быть вопросов, поэтому не проверяем на знание ответа.
                            discourse_rel = self.calc_discourse_relevance(fact, session)
                            generated_replicas.append(
                                (fact, fact_sim * discourse_rel * base_weight, 'generate_with_common_phrases(2)'))

        return generated_replicas

    def apply_insteadof_rule(self, insteadof_rules, story_rules, bot, session, interlocutor, interpreted_phrase):
        # Выполним сначала высокоприоритетные правила
        for rule in insteadof_rules:
            if rule.priority > 1.0:
                if not session.is_rule_activated(rule):
                    rule_result = rule.execute(bot, session, interlocutor, interpreted_phrase, self)
                    if rule_result.condition_success:
                        return InsteadofRuleResult.GetTrueInsteadof(rule_result.replica_is_generated)

        # Теперь выполним правила, сгенерированные из диалогов
        if story_rules:
            threshold = 0.7  # TODO - брать из конфига бота
            prev_bot_phrase = session.get_last_bot_utterance()
            if prev_bot_phrase:
                best_phrases, best_rels = self.synonymy_detector.get_most_similar(prev_bot_phrase.interpretation,
                                                                                  story_rules.get_keyphrases3(),
                                                                                  self.text_utils,
                                                                                  nb_results=10)

                # Выбираем рандомно один из вариантов. Рандомность обеспечим
                # подмешиванием небольшого шума к полученным оценкам близости.
                rx = list(filter(lambda z: z[0]>=threshold, zip(best_rels, best_phrases)))
                if len(rx) > 0:
                    rx = sorted(rx, key=lambda z: -z[0]+random.random()*0.02)
                    best_keyphrase = rx[0][1]
                    best_rules = story_rules.get_rules3_by_keyphrase(best_keyphrase)
                    best_rule = random.choice(best_rules)

                    rule_result = best_rule.execute(bot, session, interlocutor, interpreted_phrase, self)
                    if rule_result.condition_success:
                        return InsteadofRuleResult.GetTrueOther(rule_result.replica_is_generated)

            # Пробуем правила A -> B
            srt = ' '.join(interpreted_phrase.raw_tokens) if interpreted_phrase.raw_tokens else ''
            best_phrases, best_rels = self.synonymy_detector.get_most_similar(srt,
                                                                              story_rules.get_keyphrases2(),
                                                                              self.text_utils,
                                                                              nb_results=10)

            rx = list(filter(lambda z: z[0] >= threshold, zip(best_rels, best_phrases)))
            if len(rx) > 0:
                rx = sorted(rx, key=lambda z: -z[0]+random.random()*0.02)
                best_keyphrase = rx[0][1]
                best_rules = story_rules.get_rules2_by_keyphrase(best_keyphrase)
                best_rule = random.choice(best_rules)

                rule_result = best_rule.execute(bot, session, interlocutor, interpreted_phrase, self)
                if rule_result.condition_success:
                    return InsteadofRuleResult.GetTrueOther(rule_result.replica_is_generated)

        # Теперь остальные правила, с приоритетом 1 и ниже
        for rule in insteadof_rules:
            if rule.priority <= 1.0:
                if not session.is_rule_activated(rule):
                    rule_result = rule.execute(bot, session, interlocutor, interpreted_phrase, self)
                    if rule_result.condition_success:
                        return InsteadofRuleResult.GetTrueInsteadof(rule_result.replica_is_generated)

        # Ни одно из правил в insteadof_rules не подошло.
        return InsteadofRuleResult.GetFalse()

    def apply_after_rule(self, after_rules, bot, session, interlocutor, interpreted_phrase):
        # Выполним сначала высокоприоритетные правила
        for rule in after_rules:
            if rule.priority > 1.0:
                if not session.is_rule_activated(rule):
                    rule_result = rule.execute(bot, session, interlocutor, interpreted_phrase, self)
                    if rule_result.condition_success:
                        return

        # Теперь остальные правила, с приоритетом 1 и ниже
        for rule in after_rules:
            if rule.priority <= 1.0:
                if not session.is_rule_activated(rule):
                    rule_result = rule.execute(bot, session, interlocutor, interpreted_phrase, self)
                    if rule_result.condition_success:
                        return

        # Ни одно из правил в after_rules не подошло.
        return

    def run_smalltalk_action(self, rule, condition_matching_results, bot, session, interlocutor, phrase, weight_factor):
        generated_replicas = []
        if rule.is_generator():
            # Используем скомпилированную грамматику для генерации фраз..
            words = phrase.split()
            all_generated_phrases = rule.compiled_grammar.generate(words, self.text_utils.known_words)
            if len(all_generated_phrases) > 0:
                # Уберем вопросы, которые мы уже задавали, оставим top
                top = sorted(all_generated_phrases, key=lambda z: -z.get_rank())[:50]
                top = list(filter(lambda z: session.count_bot_phrase(z.get_str()) == 0, top))

                # Выберем рандомно одну из фраз
                px = [z.get_rank() for z in top]
                sum_p = sum(px)
                px = [p / sum_p for p in px]
                best = np.random.choice(top, 1, p=px)[0]
                replica = best.get_str()

                discourse_rel = self.calc_discourse_relevance(replica, session)

                if not self.bot_replica_already_uttered(bot, session, replica):
                    # проверить, если f является репликой-ответом: знает
                    # ли бот ответ на этот вопрос.
                    good_replica = True
                    if replica[-1] == u'?':
                        if self.does_bot_know_answer(replica, bot, session, interlocutor):
                            good_replica = False

                    if good_replica:
                        generated_replicas.append((replica,
                                                   best.get_rank() * discourse_rel * weight_factor,
                                                   'assertion(1)'))
        else:
            # Текст формируемой реплики указан буквально.
            for replica in rule.answers:
                assert(isinstance(replica, str))
                if condition_matching_results:
                    replica = substitute_bound_variables(SayingPhrase(replica), condition_matching_results, self.text_utils)

                if not self.bot_replica_already_uttered(bot, session, replica):
                    # проверить, если f является репликой-ответом: знает
                    # ли бот ответ на этот вопрос.
                    good_replica = True
                    if replica[-1] == u'?':
                        if self.does_bot_know_answer(replica, bot, session, interlocutor):
                            good_replica = False

                    if good_replica:
                        discourse_rel = self.calc_discourse_relevance(replica, session)
                        generated_replicas.append((replica,
                                                   discourse_rel * weight_factor,
                                                   'assertion(2)'))

        return generated_replicas

    def generate_smalltalk_replica(self, smalltalk_rules, bot, session, interlocutor):
        generated_replicas = []  # список кортежей (подобранная_реплика_бота, вес_реплики)

        if bot.enable_smalltalk and bot.has_scripting():
            # подбираем подходящую реплику в ответ на не-вопрос собеседника (обычно это
            # ответ на наш вопрос, заданный ранее).

            text_rules = smalltalk_rules.enumerate_text_rules()
            complex_rules = smalltalk_rules.enumerate_complex_rules()

            interlocutor_phrases = session.get_interlocutor_phrases(questions=True, assertions=True)
            for phrase, timegap in interlocutor_phrases[:1]:  # 05.06.2019 берем одну последнюю фразу
                time_decay = math.exp(-timegap)  # штрафуем фразы, найденные для более старых реплик
                # Проверяем условия для сложных правил.
                for rule in complex_rules:
                    matching = rule.check_condition(bot, session, interlocutor, phrase, self)
                    if matching.success:
                        rx = self.run_smalltalk_action(rule, matching, bot, session, interlocutor, phrase.interpretation, time_decay)
                        generated_replicas.extend(rx)

                        for output_phrase in rx:
                            self.logger.debug('generate_smalltalk_replica::1 rule="%s" input="%s" output="%s"', rule.get_name(), phrase, output_phrase)

                        break

                # Правила с кондиктором text проверяем все сразу для эффективности.
                cx = [(item.get_condition_text(), -1, -1) for item in text_rules]
                if cx:
                    best_premise, best_rel = self.synonymy_detector.get_most_similar(phrase.interpretation, cx, self.text_utils)
                else:
                    best_premise = None
                    best_rel = 0.0

                if best_rel > 0.7:  # TODO - брать из конфига бота
                    for rule in text_rules:
                        if rule.get_condition_text() == best_premise:
                            # Используем это правило для генерации реплики.
                            # Правило может быть простым, с явно указанной фразой, либо
                            # содержать набор шаблонов генерации.
                            rx = self.run_smalltalk_action(rule, None, bot, session, interlocutor, phrase.interpretation, best_rel * time_decay)
                            generated_replicas.extend(rx)

                            for output_phrase in rx:
                                self.logger.debug('generate_smalltalk_replica::2 rule="%s" input="%s" output="%s"',
                                                  rule.get_name(),
                                                  phrase.interpretation,
                                                  output_phrase)

                            break
                else:
                    # Проверяем smalltalk-правила, использующие intent фразы или другие условия
                    intent_rule_applied = False
                    last_interlocutor_utterance = session.get_last_interlocutor_utterance()
                    for rule in complex_rules:
                        matching = rule.check_condition(bot, session, interlocutor, last_interlocutor_utterance, self)
                        if matching.success:
                            intent_rule_applied = True
                            rx = self.run_smalltalk_action(rule, matching, bot, session, interlocutor, phrase.interpretation, time_decay)
                            generated_replicas.extend(rx)
                            for output_phrase in rx:
                                self.logger.debug('generate_smalltalk_replica::3 rule="%s" input="%s" output="%s"',
                                                  rule.get_name(),
                                                  phrase.interpretation,
                                                  output_phrase)


                    if not intent_rule_applied:
                        list2 = self.generate_with_common_phrases(bot, session, interlocutor, phrase.interpretation, time_decay)
                        generated_replicas.extend(list2)

                        if len(list2) == 0:
                            # Используем генеративную грамматику для получения возможных реплик
                            list3 = self.generate_with_generative_grammar(bot, session, interlocutor, phrase.interpretation,
                                                                          time_decay)
                            generated_replicas.extend(list3)
                            for output_phrase in list3:
                                self.logger.debug('generate_smalltalk_replica::4 input="%s" output="%s"',
                                                  phrase.interpretation,
                                                  output_phrase)


            # пробуем найти среди вопросов, которые задавал человек-собеседник недавно,
            # максимально близкие к вопросам в smalltalk базе.
            if False:
                smalltalk_utterances = set()
                for item in smalltalk_phrases:
                    smalltalk_utterances.update(item.answers)

                interlocutor_phrases = session.get_interlocutor_phrases(questions=True, assertions=False)
                for phrase, timegap in interlocutor_phrases:
                    # Ищем ближайшие реплики для данной реплики человека phrase
                    similar_items = self.synonymy_detector.get_most_similar(phrase,
                                                                            [(s, -1, -1) for s in smalltalk_utterances],
                                                                            self.text_utils,
                                                                            nb_results=5
                                                                            )
                    for replica, rel in similar_items:
                        if session.count_bot_phrase(replica) == 0:
                            time_decay = math.exp(-timegap)
                            generated_replicas.append((replica, rel * 0.9 * time_decay, 'debug3'))

            # 11-10-2020
            # используем внешний веб-сервис чит-чата
            chitchat_replicas = self.query_chitchat_service(bot, session, interlocutor, phrase)
            if chitchat_replicas:
                generated_replicas.extend(chitchat_replicas)


        # Теперь среди подобранных реплик бота в generated_replicas выбираем
        # одну, учитывая их вес.
        if len(generated_replicas) > 0:
            replica_px = [z[1] for z in generated_replicas]
            replicas = list(map(operator.itemgetter(0), generated_replicas))
            sum_p = sum(replica_px)  # +1e-7
            replica_px = [p / sum_p for p in replica_px]
            replica = np.random.choice(replicas, p=replica_px)
            return replica

        return None

    def query_chitchat_service(self, bot, session, interlocutor, last_phrase):
        res = []

        if self.chitchat_base_url:
            try:
                # 16-10-2020
                # Собираем контекст.
                # Если последняя фраза проинтерпретирована и есть пред. реплика бота - добавляем в контекст эту реплику бота
                # Если последняя фраза проинтерпретирована и нет пред. реплики бота - берем результат интерпретации
                context = last_phrase.raw_phrase
                if Jaccard_SynonymyDetector.jaccard(last_phrase.interpretation, last_phrase.raw_phrase, 3) < 0.95:
                    # фраза проинтерпретирована.
                    last_bot_phrase = session.get_last_bot_utterance()
                    if last_bot_phrase is not None:
                        context = last_bot_phrase.raw_phrase + ' | ' + last_phrase.raw_phrase

                qurl = self.chitchat_base_url.format(context)
                self.logger.debug('query_chitchat_service qurl="{}"'.format(qurl))
                response = requests.get(qurl)
                # todo потом должен быть json
                if response.ok:
                    rtext = response.text
                    self.logger.debug('query_chitchat_service response.text="{}"'.format(rtext))
                    res.append((rtext, 1.0, 'query_chitchat_service'))
            except Exception as ex:
                self.logger.error(ex)

        return res

    def cancel_all_running_items(self, bot, interlocutor):
        session = self.get_session(bot, interlocutor)
        #session.get_status()
        session.cancel_all_running_items()

    def reset_session(self, bot, interlocutor):
        session = self.get_session(bot, interlocutor)
        session.reset_history()

    def reset_usage_stat(self):
        self.paraphraser.reset_usage_stat()

    def push_phrase(self, bot, interlocutor, phrase, internal_issuer=False, force_question_answering=False):
        self.logger.info(u'push_phrase interlocutor="%s" phrase="%s"', interlocutor, phrase)
        assert(isinstance(phrase, str))
        question = self.text_utils.canonize_text(phrase)
        if question == u'#traceon':
            self.trace_enabled = True
            return
        elif question == u'#traceoff':
            self.trace_enabled = False
            return
        elif question == u'#facts':
            for fact, person, fact_id in bot.facts.enumerate_facts(interlocutor):
                print(u'{}'.format(fact))
            return

        session = self.get_session(bot, interlocutor)

        if not internal_issuer and (question in ('', '?')):
            # Пустая фраза или одиночный ? имитирует ситуацию таймаута - пользователь долгое время ничего не отвечает...
            self.continue_dialogue(bot, session, self.text_utils)
            return

        # Выполняем интерпретацию фразы с учетом ранее полученных фраз,
        # так что мы можем раскрыть анафору, подставить в явном виде опущенные составляющие и т.д.,
        # определить, является ли фраза вопросом, фактом или императивным высказыванием.
        interpreted_phrases = self.interpret_phrase(bot, session, question, internal_issuer)

        for interpreted_phrase in interpreted_phrases:
            #if force_question_answering:
                # В случае, если наш бот должен считать все входные фразы вопросами,
                # на которые он должен отвечать.
            #    interpreted_phrase.set_modality(ModalityDetector.question, interpreted_phrase.person)

            # Утверждения для 2го лица, то есть относящиеся к профилю чатбота, будем
            # рассматривать как вопросы. Таким образом, запрещаем прямой вербальный
            # доступ к профилю чатбота на запись.
            is_question2 = interpreted_phrase.is_assertion and interpreted_phrase.person == 2

            # Интерпретация фраз и в общем случае реакция на них зависит и от истории
            # общения, поэтому результат интерпретации сразу добавляем в историю.
            session.add_phrase_to_history(interpreted_phrase)

            input_processed = False
            if interpreted_phrase.is_assertion and not is_question2:
                self.logger.debug(u'Processing as assertion: "%s"', interpreted_phrase.interpretation)

                # Обработка прочих фраз. Обычно это просто утверждения (новые факты, болтовня).
                # Пробуем применить общие правила, которые опираются в том числе на
                # intent реплики или ее текст.
                #input_processed = bot.apply_rule(session, interlocutor, interpreted_phrase)
                if not session.get_status() and bot.has_scripting():
                    if bot.get_scripting().get_insteadof_rules():
                        insteadof_rule_result = self.apply_insteadof_rule(bot.get_scripting().get_insteadof_rules(),
                                                                          bot.get_scripting().get_story_rules(),
                                                                          bot,
                                                                          session,
                                                                          interlocutor,
                                                                          interpreted_phrase)
                        input_processed = insteadof_rule_result.insteadof_applied

                # 23-08-2020
                if interpreted_phrase.person == 1:
                    # Если собес сообщает о себе факт, синоним которого есть для бота в БД,
                    # то выдадим что-то вроде "и я тоже ... !"
                    # H: я люблю компьютеры!
                    # B: и я люблю компьютеры!
                    s1 = self.interpreter.denormalize_person(interpreted_phrase.interpretation, self.text_utils)
                    similar_fact = self.find_similar_fact(s1, bot, session, interlocutor)
                    if similar_fact:
                        self.logger.debug('similar fact="%s" for phrase="%s", resulting in style="same_for_me"', similar_fact, s1)
                        s2 = self.paraphraser.conditional_paraphrase(similar_fact, ['same_for_me'], self.text_utils)
                        self.say(bot, session, s2)
                    # если для факта, сообщенного собесом, есть оппозитный для бота, то выдадим "а я нет"
                    contradictory_fact = self.find_contradictory_fact(s1, bot, session, interlocutor)
                    if contradictory_fact:
                        self.logger.debug('contradictory fact="%s" for phrase="%s", resulting in style="opposite_for_me"', contradictory_fact, s1)
                        s2 = self.paraphraser.conditional_paraphrase(contradictory_fact, ['opposite_for_me'], self.text_utils)
                        self.say(bot, session, s2)
                        #input_processed = True

                elif interpreted_phrase.person not in (1, 2):
                    # Если собес сообщает факт о третьем лице, и синонимичный факт уже есть в БД,
                    # то выдадим что-то типа "я уже знаю, что ..."
                    s1 = interpreted_phrase.interpretation
                    similar_fact = self.find_similar_fact(s1, bot, session, interlocutor)
                    if similar_fact:
                        self.logger.debug('Found similar fact="%s" for phrase="%s", resulting in style "already_known"', similar_fact, s1)
                        s2 = self.paraphraser.conditional_paraphrase(similar_fact, ['already_known'], self.text_utils)
                        self.say(bot, session, s2)
                        input_processed = True  # не будем сохранять синонимичный факт в БД
                    # если для факта, сообщенного собесом, есть оппозитный для 3-го лица, то выдадим "а я нет"
                    #contradictory_fact = self.find_contradictory_fact(s1, bot, session, interlocutor)
                    #if contradictory_fact:
                    #    self.logger.debug('contradictory fact="%s" for phrase="%s", resulting in style="opposite_for_me"', contradictory_fact, s1)
                    #    s2 = self.paraphraser.conditional_paraphrase(contradictory_fact, ['opposite_for_me'], self.text_utils)
                    #    self.say(bot, session, s2)

                # TODO: в принципе возможны два варианта последствий срабатывания
                # правил. 1) считаем, что правило полностью выполнило все действия для
                # утверждения, в том числе сохранило в базе знаний новый факт, если это
                # необходимо. 2) полагаем, что правило что-то сделало, но факт в базу мы должны
                # добавить сами.
                # Возможно, надо явно задавать в правилах эти особенности (INSTEAD-OF или BEFORE)
                # Пока считаем, что правило сделало все, что требовалось.

                if not input_processed:
                    # Утверждение добавляем как факт в базу знаний, в раздел для
                    # текущего собеседника.
                    # TODO: факты касательно третьих лиц надо вносить в общий раздел базы, а не
                    # для текущего собеседника.
                    fact_person = '3'
                    fact = interpreted_phrase.interpretation
                    if self.trace_enabled:
                        self.logger.info(u'Adding "%s" to knowledge base', fact)
                    bot.facts.store_new_fact(interlocutor, (fact, fact_person, '--from dialogue--'), False)

            insteadof_rule_result = None
            was_running_session = False

            if not input_processed and session.get_status():
                was_running_session = True
                if not interpreted_phrase.is_question:
                    if isinstance(session.get_status(), RunningFormStatus):
                        # Продолжается обработка вербальной формы.
                        # Был задан вопрос для заполнения поля формы.
                        # Для простоты считаем, что пользователь ответил нормально и его ответ
                        # можно использовать для заполнения
                        running_form = session.get_status()
                        if running_form.current_field.source == 'entity':
                            field_value = bot.extract_entity_from_str(running_form.current_field.from_entity, interpreted_phrase.raw_phrase)
                            if not field_value:
                                # для простоты считаем, что весь исходный ответ пользователя заполняет поле формы.
                                field_value = interpreted_phrase.raw_phrase
                            running_form.fields[running_form.current_field.name] = field_value
                        elif running_form.current_field.source == 'raw_response':
                            field_value = interpreted_phrase.raw_phrase
                            running_form.fields[running_form.current_field.name] = field_value
                        else:
                            raise NotImplementedError()

                        # Остались еще незаполненные поля?
                        for field in running_form.form.fields:
                            if field.name not in running_form.fields:
                                # Зададим вопрос для заполнения поля
                                running_form.set_current_field(field)
                                bot.say(session, field.question)
                                return

                        # Все поля заполнены
                        self.form_ok(bot, session, interlocutor)
                        return
                    elif isinstance(session.get_status(), RunningScenario):
                        # Если в сценарии есть правила
                        do_next_step = True
                        rule_applied = False
                        rules = session.get_status().get_insteadof_rules()
                        if rules:
                            insteadof_rule_result = self.apply_insteadof_rule(rules,
                                                                              None,  # story_rules
                                                                              bot,
                                                                              session,
                                                                              interlocutor,
                                                                              interpreted_phrase)
                            rule_applied = insteadof_rule_result.is_any_applied()
                            if rule_applied:
                                if session.get_output_buffer_phrase():
                                    if session.get_output_buffer_phrase()[-1] == '?':
                                        # insteadof-правило сгенерировано вопрос, поэтому следующий шаг сценария
                                        # пока не запускаем
                                        do_next_step = False

                        if not rule_applied:
                            # TODO
                            # Сценарий может содержать код обработки некоторых вопросов.
                            pass

                        replica = None
                        if not rule_applied:
                            # генерируем smalltalk-реплику для текущего контекста
                            if bot.enable_smalltalk:
                                if session.get_status().get_smalltalk_rules():
                                    replica = self.generate_smalltalk_replica(session.get_status().get_smalltalk_rules(),
                                                                              bot, session, interlocutor)
                                else:
                                    replica = self.generate_smalltalk_replica(bot.get_scripting().get_smalltalk_rules(),
                                                                              bot, session, interlocutor)

                            # 19.12.2020 если читчат сгенерировал вопрос, то вместо перехода на следующий шаг в сценарии
                            #            можно просто выдать этот вопрос и дождаться реакции пользователя.
                            if replica[-1] == '?':
                                x = session.get_status().get_remaining_chitchat_questions_per_step()
                                if x > 0:
                                    session.add_to_buffer(replica)
                                    #do_next_step = False
                                    #replica = None
                                    return

                        # Отрабатывает шаг сценария
                        if do_next_step:
                            self.run_scenario_step(bot, session, interlocutor, interpreted_phrase)

                        if replica:
                            if replica[-1] == '?':
                                # Если smalltalk реплика является вопросом, то надо проверить,
                                # что шаг сценария не сгенерировал тоже вопрос. Два вопроса подряд от бота
                                # мы не будем выдавать, оставим только вопрос сценария.
                                if session.get_output_buffer_phrase():
                                    if session.get_output_buffer_phrase()[-1] != '?':
                                        #session.add_to_buffer(replica)
                                        self.say(bot, session, replica)
                                else:
                                    #session.add_to_buffer(replica)
                                    self.say(bot, session, replica)
                            else:
                                # Вставляем эту реплику перед фразой шага, чтобы вопрос сценария не был
                                # экранирован smalltalk-репликой.
                                #session.insert_into_buffer(replica)
                                self.say_before_b(bot, session, replica)

                        if rule_applied:
                            return
                else:
                    # В некоторых случаях мы можем обрабатывать какие-то вопросы внутри сценария.
                    if isinstance(session.get_status(), RunningScenario):
                        running_scenario = session.get_status()
                        scenario = running_scenario.scenario
                        if scenario.can_process_questions():
                            question_answered = scenario.process_question(running_scenario, bot, session, interlocutor,
                                                                          interpreted_phrase, self.text_utils)

                            if question_answered:
                                return

            self.discourse.process_interrogator_phrase(bot, session, interpreted_phrase)
            if interpreted_phrase.is_imperative:
                self.logger.debug(u'Processing as imperative: "%s"', interpreted_phrase.interpretation)
                # Обработка приказов (императивов).
                order_processed = self.process_order(bot, session, interlocutor, interpreted_phrase)
                if not order_processed:
                    # Сообщим, что не знаем как обработать приказ.
                    answer = self.premise_not_found.order_not_understood(phrase, bot, self.text_utils)
                    self.logger.debug('"Order not processed" handler: "%s"', answer)
                    self.say(bot, session, answer)
                    order_processed = True
            elif interpreted_phrase.is_question or is_question2:
                self.logger.debug(u'Processing as question: "%s"', interpreted_phrase.interpretation)

                replica = None
                input_processed = False

                #if not session.get_status() and bot.has_scripting():
                if bot.has_scripting():
                    if session.get_status():
                        insteadof_rules = session.get_status().get_insteadof_rules()
                        story_rules = None
                    else:
                        insteadof_rules = bot.get_scripting().get_insteadof_rules()
                        story_rules = bot.get_scripting().get_story_rules()

                    if insteadof_rules:
                        insteadof_rule_result = self.apply_insteadof_rule(insteadof_rules,
                                                                          story_rules,
                                                                          bot,
                                                                          session,
                                                                          interlocutor,
                                                                          interpreted_phrase)
                        input_processed = insteadof_rule_result.is_any_applied()

                if not input_processed:
                    # Обрабатываем вопрос собеседника (либо результат трансляции императива).
                    answers = self.build_answers(session, bot, interlocutor, interpreted_phrase)
                    for answer in answers:
                        self.say(bot, session, answer)

                    # В некоторых случаях генерация реплики после ответа может быть нежелательна,
                    # например для FAQ-бота. Поэтому используем флаг в конфиге бота.
                    if bot.replica_after_answering:
                        # Возможно, кроме ответа на вопрос, надо выдать еще какую-то реплику.
                        # Например, для смены темы разговора.
                        replica_generated = False
                        if len(answers) > 0 and bot.has_scripting():
                            additional_speech = bot.scripting.generate_after_answer(bot,
                                                                                    self,
                                                                                    interlocutor,
                                                                                    interpreted_phrase,
                                                                                    answers[-1])
                            if additional_speech is not None:
                                self.say(bot, session, additional_speech)
                                replica_generated = True

                        if not replica_generated:
                            replica = self.generate_smalltalk_replica(bot, session, interlocutor)
                            if replica:
                                self.say(bot, session, replica)
                            replica = None
            elif interpreted_phrase.is_assertion:
                # Теперь генерация реплики для случая, когда реплика собеседника - не-вопрос.
                # 13.07.2019 если применено INSTEADOF-правило, но оно не сгенерировало никакую ответную реплику,
                # то есть резон сказать что-то на базе common_phrases
                answer_generated = False
                answer = None
                if not was_running_session:
                    if not input_processed or (insteadof_rule_result and not insteadof_rule_result.replica_is_generated):
                        replica = self.generate_smalltalk_replica(bot.get_scripting().get_smalltalk_rules(), bot, session, interlocutor)
                        if replica:
                            answer = replica
                            answer_generated = True

                if answer_generated:
                    self.say(bot, session, answer)

            if bot.get_scripting().get_after_rules():
                self.apply_after_rule(bot.get_scripting().get_after_rules(),
                                      bot,
                                      session,
                                      interlocutor,
                                      interpreted_phrase)

            if interpreted_phrase.is_imperative:
                self.discourse.store_order_in_database(bot, session, interpreted_phrase)
            elif interpreted_phrase.is_question or is_question2:
                self.discourse.store_question_in_database(bot, session, interpreted_phrase)
            elif interpreted_phrase.is_assertion:
                self.discourse.store_assertion_in_database(bot, session, interpreted_phrase)


    def process_order(self, bot, session, interlocutor, interpreted_phrase):
        self.logger.debug(u'Processing order "%s"', interpreted_phrase.interpretation)

        # Пробуем применить общие правила, которые опираются в том числе на
        # intent реплики или ее текст.
        order_processed = False
        if bot.has_scripting():
            order_processed = self.apply_insteadof_rule(bot.get_scripting().get_insteadof_rules(),
                                                        bot.get_scripting().get_story_rules(),
                                                        bot,
                                                        session,
                                                        interlocutor,
                                                        interpreted_phrase)

        if order_processed and order_processed.is_any_applied():
            # Сработало правило.
            return True
        else:
            if bot.faq:
                best_faq_answer, best_faq_rel, best_faq_question = bot.faq.get_most_similar(
                    interpreted_phrase.interpretation,
                    self.synonymy_detector,
                    self.text_utils)
                if best_faq_rel > self.synonymy_detector.get_threshold():
                    self.logger.debug(u'Found FAQ rel=%g answer="%s"', best_faq_rel, best_faq_answer)
                    bot.say(session, best_faq_answer)
                    return True

            if True:
                # Прогоняем императив через стандартный пайплайн обработки вопросов.
                # Это позволит обрабатывать реплики типа:
                # "Расскажи про свои оценки в школе"
                answers = []
                answer_rels = []
                best_rels = None

                # Нужна ли предпосылка, чтобы ответить на вопрос?
                # Используем модель, которая вернет вероятность того, что
                # пустой список предпосылок достаточен.
                p_enough = self.enough_premises.is_enough(premise_str_list=[],
                                                          question_str=interpreted_phrase.interpretation,
                                                          text_utils=self.text_utils)
                if p_enough > 0.5:
                    # Единственный ответ можно построить без предпосылки, например для вопроса "Сколько будет 2 плюс 2?"
                    answers00, answer_rels00 = self.answer_builder.build_answer_text([u''], [1.0],
                                                                                     interpreted_phrase.interpretation,
                                                                                     self.text_utils)
                    if len(answers00) != 1:
                        self.logger.debug(u'Exactly 1 answer is expected for question={}, got {}'.format(
                            interpreted_phrase.interpretation, len(answers00)))

                    for answer, rel in zip(answers00, answer_rels00):
                        # Если в качестве ответа сгенерирован мусор (абракадабра), то уберем такой ответ из выдачи.
                        if not self.intent_detector.detect_abracadabra(answer, self.text_utils):
                            answers.append(answer)
                            answer_rels.append(rel)
                        else:
                            self.logger.debug('Answer "%s" is recognized as abracadabra, so removing it', answer)

                    best_rels = answer_rels
                else:
                    # определяем наиболее релевантную предпосылку
                    memory_phrases = list(bot.facts.enumerate_facts(interlocutor))

                    best_premises, best_rels = self.relevancy_detector.get_most_relevant(
                        interpreted_phrase.interpretation,
                        memory_phrases,
                        self.text_utils,
                        nb_results=3)
                    if self.trace_enabled:
                        if best_rels[0] >= self.min_premise_relevancy:
                            self.logger.info('Best premise is "%s" with relevancy=%f', best_premises[0], best_rels[0])

                    if len(answers) == 0:
                        if False:  #bot.premise_is_answer:
                            if best_rels[0] >= self.min_premise_relevancy:
                                # В качестве ответа используется весь текст найденной предпосылки.
                                answers = [best_premises[:1]]
                                answer_rels = [best_rels[:1]]
                        else:
                            premises2 = []
                            premise_rels2 = []

                            # 30.11.2018 будем использовать только 1 предпосылку и генерировать 1 ответ
                            if True:
                                if best_rels[0] >= self.min_premise_relevancy:
                                    premises2 = [best_premises[:1]]
                                    premise_rels2 = best_rels[:1]
                            else:
                                max_rel = max(best_rels)
                                for premise, rel in zip(best_premises[:1], best_rels[:1]):
                                    if rel >= self.min_premise_relevancy and rel >= 0.4 * max_rel:
                                        premises2.append([premise])
                                        premise_rels2.append(rel)

                            if len(premises2) > 0:
                                # генерация ответа на основе выбранной предпосылки.
                                # 28.08.2019 для вопросов к боту в качестве ответа будем выдавать полный текст найденного
                                # факта.
                                if False:  #interpreted_phrase.person == 2 and len(premises2) == 1 and len(premises2[0]) == 1:
                                    answers.append(premises2[0][0])
                                    answer_rels.append(1.0)
                                else:
                                    answers00, answer_rels00 = self.answer_builder.build_answer_text(premises2,
                                                                                                     premise_rels2,
                                                                                                     interpreted_phrase.interpretation,
                                                                                                     self.text_utils)
                                    for answer, rel in zip(answers00, answer_rels00):
                                        if not self.intent_detector.detect_abracadabra(answer, self.text_utils):
                                            answers.append(answer)
                                            answer_rels.append(rel)
                                        else:
                                            # если модель генерации ответа выдала мусор, то в качестве ответа отдадим предпосылку.
                                            premise_str = premises2[0][0]
                                            self.logger.debug(
                                                'Answer "%s" is recognized as abracadabra, so returning premise "%s" as an answer',
                                                answer, premise_str)
                                            answers.append(premise_str)
                                            answer_rels.append(rel * 0.99)

                if answers:
                    # Выберем один самый достоверный ответ.
                    best_answer = None
                    best_rel = 0.0
                    for answer, rel in zip(answers, answer_rels):
                        if rel > best_rel:
                            best_answer = answer
                            best_rel = rel

                    bot.say(session, best_answer)
                    return True

            if self.chitchat_base_url:
                chitchat_replicas = self.query_chitchat_service(bot, session, interlocutor, interpreted_phrase)
                if chitchat_replicas:
                    # TODO: выбирать наиболее уместную реплику
                    answer = chitchat_replicas[0][0]
                    bot.say(session, answer)
                    return True

            if self.premise_not_found.get_noanswer_rules():
                res = self.apply_insteadof_rule(self.premise_not_found.get_noanswer_rules(),
                                                None, #bot.get_scripting().get_story_rules(),
                                                bot, session, interlocutor, interpreted_phrase)
                if res.is_any_applied():
                    return True

            return bot.process_order(session, interlocutor, interpreted_phrase)

    def apply_rule(self, bot, session, interpreted_phrase):
        return bot.apply_rule(session, interpreted_phrase)

    def continue_dialogue(self, bot, session, text_utils):
        phrase = bot.get_scripting().get_continuation_rules().generate_phrase(bot, session, self)
        if phrase:
            bot.say(session, phrase)

    def premise_not_found(self, phrase, bot, text_utils):
        return self.premise_not_found.generate_answer(phrase, bot, text_utils)

    def build_answers0(self, session, bot, interlocutor, interpreted_phrase):
        if self.trace_enabled:
            self.logger.debug(u'Question to process="%s"', interpreted_phrase.interpretation)

        # Проверяем базу FAQ, вдруг там есть развернутый ответ на вопрос.
        best_faq_answer = None
        best_faq_rel = 0.0
        best_faq_question = None
        if bot.faq:
            best_faq_answer, best_faq_rel, best_faq_question = bot.faq.get_most_similar(interpreted_phrase.interpretation,
                                                                                        self.synonymy_detector,
                                                                                        self.text_utils)

        answers = []
        answer_rels = []
        best_rels = None

        # Нужна ли предпосылка, чтобы ответить на вопрос?
        # Используем модель, которая вернет вероятность того, что
        # пустой список предпосылок достаточен.
        p_enough = self.enough_premises.is_enough(premise_str_list=[],
                                                  question_str=interpreted_phrase.interpretation,
                                                  text_utils=self.text_utils)
        if p_enough > 0.5:
            # Единственный ответ можно построить без предпосылки, например для вопроса "Сколько будет 2 плюс 2?"
            answers00, answer_rels00 = self.answer_builder.build_answer_text([u''], [1.0],
                                                                         interpreted_phrase.interpretation,
                                                                         self.text_utils)
            if len(answers00) != 1:
                self.logger.debug(u'Exactly 1 answer is expected for question={}, got {}'.format(interpreted_phrase.interpretation, len(answers00)))

            for answer, rel in zip(answers00, answer_rels00):
                # Если в качестве ответа сгенерирован мусор (абракадабра), то уберем такой ответ из выдачи.
                if not self.intent_detector.detect_abracadabra(answer, self.text_utils):
                    answers.append(answer)
                    answer_rels.append(rel)
                else:
                    self.logger.debug('Answer "%s" is recognized as abracadabra, so removing it', answer)

            best_rels = answer_rels
        else:
            # определяем наиболее релевантную предпосылку
            memory_phrases = list(bot.facts.enumerate_facts(interlocutor))

            best_premises, best_rels = self.relevancy_detector.get_most_relevant(interpreted_phrase.interpretation,
                                                                                 memory_phrases,
                                                                                 self.text_utils,
                                                                                 nb_results=3)
            if self.trace_enabled:
                if best_rels[0] >= self.min_premise_relevancy:
                    self.logger.info('Best premise is "%s" with relevancy=%f', best_premises[0], best_rels[0])

            if len(answers) == 0:
                if bot.premise_is_answer:
                    if best_rels[0] >= self.min_premise_relevancy:
                        # В качестве ответа используется весь текст найденной предпосылки.
                        answers = [best_premises[:1]]
                        answer_rels = [best_rels[:1]]
                else:
                    premises2 = []
                    premise_rels2 = []

                    # 30.11.2018 будем использовать только 1 предпосылку и генерировать 1 ответ
                    if True:
                        if best_rels[0] >= self.min_premise_relevancy:
                            premises2 = [best_premises[:1]]
                            premise_rels2 = best_rels[:1]
                    else:
                        max_rel = max(best_rels)
                        for premise, rel in zip(best_premises[:1], best_rels[:1]):
                            if rel >= self.min_premise_relevancy and rel >= 0.4 * max_rel:
                                premises2.append([premise])
                                premise_rels2.append(rel)

                    if len(premises2) > 0:
                        # генерация ответа на основе выбранной предпосылки.
                        # 28.08.2019 для вопросов к боту в качестве ответа будем выдавать полный текст найденного
                        # факта.
                        if interpreted_phrase.person == 2 and len(premises2) == 1 and len(premises2[0]) == 1:
                            answers.append(premises2[0][0])
                            answer_rels.append(1.0)
                        else:
                            answers00, answer_rels00 = self.answer_builder.build_answer_text(premises2, premise_rels2,
                                                                                             interpreted_phrase.interpretation,
                                                                                             self.text_utils)
                            for answer, rel in zip(answers00, answer_rels00):
                                if not self.intent_detector.detect_abracadabra(answer, self.text_utils):
                                    answers.append(answer)
                                    answer_rels.append(rel)
                                else:
                                    # если модель генерации ответа выдала мусор, то в качестве ответа отдадим предпосылку.
                                    premise_str = premises2[0][0]
                                    self.logger.debug('Answer "%s" is recognized as abracadabra, so returning premise "%s" as an answer', answer, premise_str)
                                    answers.append(premise_str)
                                    answer_rels.append(rel * 0.99)

                if len(answers) == 0:
                    # Попробуем использовать 2 последних утверждения собеседника как предпосылки.
                    last_h_entries = session.get_interlocutor_phrases(questions=False, assertions=True, last_nb=2)
                    if len(last_h_entries) == 2:
                        last_h_phrases = [z[0] for z in last_h_entries]
                        premise1 = last_h_phrases[0].interpretation
                        premise2 = last_h_phrases[1].interpretation
                        question = interpreted_phrase.interpretation
                        rel = self.p2q_relevancy.calc_relevancy(premise1, premise2, question, self.text_utils)
                        if rel > self.min_premise_relevancy:
                            self.logger.debug('P(2)Q with premise1="%s" premise2="%s" question="%s" rel=%g', premise1,
                                              premise2, question, rel)
                            best_rels = [rel]
                            premises2 = [[premise1, premise2]]
                            premise_rels2 = [rel]
                            answers00, answer_rels00 = self.answer_builder.build_answer_text(premises2, premise_rels2,
                                                                                             interpreted_phrase.interpretation,
                                                                                             self.text_utils)
                            for answer, rel in zip(answers00, answer_rels00):
                                if not self.intent_detector.detect_abracadabra(answer, self.text_utils):
                                    answers.append(answer)
                                    answer_rels.append(rel)
                                else:
                                    self.logger.debug('Answer "%s" is recognized as abracadabra, so removing it', answer)


        if len(best_rels) == 0 or (best_faq_rel > best_rels[0] and best_faq_rel > self.min_faq_relevancy):
            # Если FAQ выдал более достоверный ответ, чем генератор ответа, или если
            # генератор ответа вообще ничего не выдал (в базе фактов пусто), то возьмем
            # текст ответа из FAQ.
            answers = [best_faq_answer]
            answer_rels = [best_faq_rel]
            self.logger.info(u'FAQ entry provides nearest question="%s" with rel=%e', best_faq_question, best_faq_rel)

        if len(answers) == 0:
            # Не удалось найти предпосылку для формирования ответа.

            # Попробуем обработать вопрос правилами.
            res = InsteadofRuleResult.GetFalse()
            if self.chitchat_base_url is None:
                if self.premise_not_found.get_noanswer_rules():
                    res = self.apply_insteadof_rule(self.premise_not_found.get_noanswer_rules(),
                                                    None, #bot.get_scripting().get_story_rules(),
                                                    bot, session, interlocutor, interpreted_phrase)

            if not res.is_any_applied():
                # ???? вроде уже отработали INSTEAD-OF RULES
                res = self.apply_insteadof_rule(bot.get_scripting().get_insteadof_rules(),
                                                bot.get_scripting().get_story_rules(),
                                                bot, session, interlocutor, interpreted_phrase)
            if not res.is_any_applied():
                # Правила не сработали, значит выдаем реплику "Информации нет"
                # 12-10-2020 меняем тактику в случае доступности веб-сервиса читчата.
                self.premise_not_found_count += 1
                answer = None

                do_query_chitchat = False

                # Стратегия №1: всегда предпочитаем вызывать сервис читчата, если он доступен,
                # так как он дает более разнообразные и контекстные ответы, чем правила.
                if self.chitchat_base_url:
                    do_query_chitchat = True

                # Стратегия №2: после первой реплики "не знаю" начинаем использовать веб-сервис чит-чата
                # с некоторой частотой.
                #if self.premise_not_found_count > 1:
                #    if random.random() > math.exp(-self.premise_not_found_count):
                #        do_query_chitchat = True

                if do_query_chitchat:
                    chitchat_replicas = self.query_chitchat_service(bot, session, interlocutor, interpreted_phrase)
                    if chitchat_replicas:
                        answer = chitchat_replicas[0][0]

                if answer is None:
                    answer = self.premise_not_found.generate_answer(interpreted_phrase.interpretation,
                                                                    bot,
                                                                    self.text_utils)

                answers.append(answer)
                answer_rels.append(1.0)

        return answers, answer_rels

    def build_answers(self, session, bot, interlocutor, interpreted_phrase):
        answers, answer_confidenses = self.build_answers0(session, bot, interlocutor, interpreted_phrase)
        if len(answer_confidenses) == 0 or max(answer_confidenses) < self.min_premise_relevancy:
            # тут нужен алгоритм генерации ответа в условиях, когда
            # у бота нет нужных фактов. Это может быть как ответ "не знаю",
            # так и вариант "нет" для определенных категорий вопросов.
            if False:  #bot.has_scripting():
                answer = bot.scripting.buid_answer(self, interlocutor, interpreted_phrase)
                answers = [answer]

        return answers

    def pop_phrase(self, bot, interlocutor):
        session = self.get_session(bot, interlocutor)
        return session.extract_from_buffer()

    def get_session(self, bot, interlocutor):
        return self.session_factory.get_session(bot, interlocutor)

    def get_synonymy_detector(self):
        return self.synonymy_detector

    def get_text_utils(self):
        return self.text_utils

    def get_word_embeddings(self):
        return self.text_utils.word_embeddings
