# -*- coding: utf-8 -*-
"""
Реализация классификатора режима генерации ответа в чатботе на базе нейросетки.
"""

import os
import json
import numpy as np
import logging
from keras.models import model_from_json
from model_selector import ModelSelector

class NN_ModelSelector(ModelSelector):
    def __init__(self):
        super(NN_ModelSelector, self).__init__()
        self.logger = logging.getLogger('NN_ModelSelector')

    def load(self, models_folder):
        self.logger.info('Loading NN_ModelSelector model files')

        arch_filepath = os.path.join(models_folder, 'qa_model_selector.arch')
        weights_path = os.path.join(models_folder, 'qa_model_selector.weights')
        with open(arch_filepath, 'r') as f:
            m = model_from_json(f.read())

        m.load_weights(weights_path)
        self.model = m

        with open(os.path.join(models_folder, 'qa_model_selector.config'), 'r') as f:
            self.model_config = json.load(f)

        self.word_dims = self.model_config['word_dims']
        self.w2v_path = self.model_config['w2v_path']
        self.padding = self.model_config['padding']
        self.max_wordseq_len = int(self.model_config['max_inputseq_len'])
        self.X1_probe = np.zeros((1, self.max_wordseq_len, self.word_dims), dtype=np.float32)
        self.X2_probe = np.zeros((1, self.max_wordseq_len, self.word_dims), dtype=np.float32)
        self.w2v_filename = os.path.basename(self.w2v_path)

    def select_model(self, premise_str, question_str, text_utils, word_embeddings):
        # Определяем способ генерации ответа
        self.X1_probe.fill(0)
        self.X2_probe.fill(0)

        if self.padding == 'left':
            premise_words = text_utils.pad_wordseq(text_utils.tokenize(premise_str), self.max_wordseq_len)
            question_words = text_utils.pad_wordseq(text_utils.tokenize(question_str), self.max_wordseq_len)
        else:
            premise_words = text_utils.rpad_wordseq(text_utils.tokenize(premise_str), self.max_wordseq_len)
            question_words = text_utils.rpad_wordseq(text_utils.tokenize(question_str), self.max_wordseq_len)

        word_embeddings.vectorize_words(self.w2v_filename, premise_words, self.X1_probe, 0)
        word_embeddings.vectorize_words(self.w2v_filename, question_words, self.X2_probe, 0)

        y_probe = self.model.predict({'input_words1': self.X1_probe, 'input_words2': self.X2_probe})

        if False:  # Отладка
            print(u'premise_words=', u' '.join(premise_words).strip())
            print(u'question_words=', u' '.join(question_words).strip())
            print('X1_probe=', self.X1_probe)
            print('X2_probe=', self.X2_probe)
            print('y_probe=', y_probe)

        model_selector = np.argmax( y_probe[0] )
        return model_selector

