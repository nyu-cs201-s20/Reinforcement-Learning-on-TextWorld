import textworld.gym
import numpy as np
import torch
import torch.nn.functional as F
from torch import optim
import re
from collections import defaultdict
from transformers import GPT2Model, GPT2Tokenizer, GPT2Config
from transformers import DistilBertModel, DistilBertTokenizer, DistilBertConfig
from typing import Iterable
import random
from collections import namedtuple, deque

class PretrainedEmbed:

    def __init__(self, words: Iterable[str], vectors: np.ndarray):
        """
        Initializes an Embeddings object directly from a list of words
        and their embeddings.

        :param words: A list of words
        :param vectors: A 2D array of shape (len(words), embedding_size)
            where for each i, vectors[i] is the embedding for words[i]
        """
        self.words = list(words)
        self.indices = {w: i for i, w in enumerate(words)}
        self.vectors = vectors

    def __len__(self):
        return len(self.words)

    def __contains__(self, word: str):
        return word in self.words

    def __getitem__(self, words: Iterable[str]):
        """
        Retrieves embeddings for a list of words.

        :param words: A list of words
        :return: A 2D array of shape (len(words), embedding_size) where
            for each i, the ith row is the embedding for words[i]
        """
        return self.vectors[[self.indices[w] for w in words]]

    @classmethod
    def from_file(cls, filename: str):
        """
        Initializes an Embeddings object from a .txt file containing
        word embeddings in GloVe format.

        :param filename: The name of the file containing the embeddings
        :return: An Embeddings object containing the loaded embeddings
        """
        with open(filename, "r") as f:
            all_lines = [line.strip().split(" ", 1) for line in f]
        words, vecs = zip(*all_lines)
        return cls(words, np.array([np.fromstring(v, sep=" ") for v in vecs]))

class SimpleAgent(textworld.gym.Agent):
    def __init__(self, agent_mode = "random", seed=None):
        self.agent_mode = agent_mode
        self.seed = seed
    
    def get_env_infos(self):
        return textworld.EnvInfos(admissible_commands=True, max_score = True)
    
    def action(self, observations, score, done, infos):
        if self.agent_mode == "random":
            if self.seed:
                np.random.seed(self.seed)
            return np.random.choice(infos["admissible_commands"])
        elif self.agent_mode == "human":
            print(observations)
            return input()
        
class AgentNetwork(torch.nn.Module):
    def __init__(self, input_size, embedding_size, hidden_size, device="cuda"):
        super(AgentNetwork, self).__init__()
        self.hidden_size  = hidden_size
        self.device = device
        self.embedding    = torch.nn.Embedding(input_size, embedding_size)
        self.gru_input  = torch.nn.GRU(embedding_size, hidden_size)
        self.gru_command  = torch.nn.GRU(embedding_size, hidden_size)
        self.gru_state    = torch.nn.GRU(hidden_size, hidden_size)
        self.hidden_state = self.init_hidden(1)
        self.linear       = torch.nn.Linear(hidden_size, 1)
        self.linear_command = torch.nn.Linear(hidden_size * 2, 1)

    def load_pretrained_embeddings(self, embeddings):
        self.embedding.weight.data[:-2, :] = torch.tensor(embeddings.vectors)
    
    def init_hidden(self, batch_size):
        self.hidden_state = torch.zeros(1, batch_size, self.hidden_size, device=self.device)
        
    def forward(self, observations, commands):
        batch_size, num_commands = observations.shape[1], commands.shape[1]
        embed_obs = self.embedding(observations)
        output_encoder, hidden_encoder = self.gru_input(embed_obs)
        output_state, self.hidden_state = self.gru_state(hidden_encoder, self.hidden_state)
        value = self.linear(output_state)

        embed_commands = self.embedding.forward(commands)
        output_commands, hidden_commands = self.gru_command.forward(embed_commands) 
        input_commands = torch.stack([self.hidden_state] * num_commands, 2)
        hidden_commands = torch.stack([hidden_commands] * batch_size, 1) 
        input_commands = torch.cat([input_commands, hidden_commands], dim=-1)

        scores = F.relu(self.linear_command(input_commands)).squeeze(-1)  
        probs = F.softmax(scores, dim=2) 
        index = probs[0].multinomial(num_samples=1).unsqueeze(0)
        
        return scores, index, value
    
    
class GPTNetwork(torch.nn.Module):
    def __init__(self, input_size, embedding_size, hidden_size, device="cuda"):
        super(GPTNetwork, self).__init__()
        self.hidden_size  = hidden_size
        self.device = device
        self.embedding    = torch.nn.Embedding(input_size, embedding_size)
        
        self.gpt_config = GPT2Config(vocab_size=input_size, max_length=32, dropout=0.0, n_embd=embedding_size, n_layer=10, n_head=10)
        self.gpt2 = GPT2Model(self.gpt_config)
        self.gpt_linear = torch.nn.Linear(embedding_size, hidden_size)
        self.linear = torch.nn.Linear(hidden_size, 1)
        
        
        self.gru_command  = torch.nn.GRU(embedding_size, hidden_size, batch_first=True)
        self.linear_command = torch.nn.Linear(hidden_size * 2, 1)

    
    def load_pretrained_embeddings(self, embeddings):
        self.embedding.weight.data[:-2, :] = torch.tensor(embeddings.vectors)
        
    def forward(self, observations, commands):
        observations = observations.permute(1,0)
        commands = commands.permute(1,0)
        batch_size, num_commands = observations.shape[0], commands.shape[0]

        embed_obs = self.embedding(observations)
        gpt2_output = self.gpt2(inputs_embeds=embed_obs)[0][:,-1,:].unsqueeze(1)
        gpt2_output = self.gpt_linear(gpt2_output)
        value = self.linear(gpt2_output)

        embed_commands = self.embedding.forward(commands)
        output_commands, hidden_commands = self.gru_command.forward(embed_commands) 
        input_commands = torch.stack([gpt2_output] * num_commands, 2)
        hidden_commands = torch.stack([hidden_commands] * batch_size, 1)
        input_commands = torch.cat([input_commands, hidden_commands], dim=-1)

        scores = F.relu(self.linear_command(input_commands)).squeeze(-1)  
        probs = F.softmax(scores, dim=2) 
        index = probs[0].multinomial(num_samples=1).unsqueeze(0)
        
        return scores, index, value

class BERT_GRU(torch.nn.Module):
    def __init__(self, input_size, hidden_size, device='cuda'):
        super(BERT_GRU, self).__init__()
        self.hidden_size  = hidden_size
        
        if torch.cuda.is_available():
            self.device = 'cuda'
        else:
            self.device = 'cpu'
        
        self.distilBERT = DistilBertModel.from_pretrained('distilbert-base-uncased')
        
        self.observation_gru = torch.nn.GRU(768, hidden_size, batch_first=True)
        self.descriptions_gru = torch.nn.GRU(768, hidden_size, batch_first=True)
        self.inventory_gru = torch.nn.GRU(768, hidden_size, batch_first=True)
        self.commands_gru = torch.nn.GRU(768, hidden_size, batch_first=True)
        
        self.linear_value = torch.nn.Linear(hidden_size * 3, 1)
        
        self.linear1_command = torch.nn.Linear(hidden_size * 4, 50)
        self.linear2_command = torch.nn.Linear(50, 1)
        
        #freeze distilBERT parameters
        for param in self.distilBERT.parameters():
            param.requires_grad = False
        
        
    def forward(self, observations, descriptions, inventory, commands):
        
        batch_size, num_commands = observations['input_ids'].shape[0], commands['input_ids'].shape[0]

        #generate encodings
        #NOTE: passing only ['CLS'] token encoding to GRU modules
        observations = self.distilBERT(input_ids=observations['input_ids'], attention_mask=observations['attention_mask'])[0][:,0,:]
        observations = self.observation_gru(observations)[0] #using output state not sure if should use hidden state
        
        descriptions = self.distilBERT(input_ids=descriptions['input_ids'], attention_mask=descriptions['attention_mask'])[0][:,0,:]
        descriptions = self.descriptions_gru(descriptions)[0]
        
        inventory = self.distilBERT(input_ids=inventory['input_ids'], attention_mask=inventory['attention_mask'])[0][:,0,:]
        inventory = self.inventory_gru(inventory)[0]
        
        commands = self.distilBERT(input_ids=commands['input_ids'], attention_mask=commands['attention_mask'])[0][:,0,:]
        commands = self.commands_gru(commands)[0]
        
        #concatenate observations, descriptions, and inventory into state encoding
        state_encoding = torch.cat((observations, descriptions, inventory), 1)
        
        #compute estimated state value
        value = self.linear_value(state_encoding)
        value = value.unsqueeze(0)
        
        #concatenate state encoding and commands encoding
        state_encoding_stack = torch.stack([state_encoding]*num_commands, dim=0)
        commands = commands.unsqueeze(1)
        state_action_encodings = torch.cat((state_encoding_stack, commands), dim=2)
        
        #pass state_action_encodings through linear and relu layers to generate scores and action probabilities
        scores = self.linear1_command(state_action_encodings)
        scores = F.relu(scores)
        scores = self.linear2_command(scores)
        scores = scores.squeeze().unsqueeze(0).unsqueeze(0)
        probs = F.softmax(scores, dim=2)
        
        #sample action index from action probabilitites
        index = probs[0].multinomial(num_samples=1).unsqueeze(0)
        
        return scores, index, value
    
Transition = namedtuple('Transition',('observation', 'commands', 'action', 'next_observation', 'next_commands', 'reward'))

class ReplayMemory(object):
    
    def __init__(self, capacity):
        super(ReplayMemory, self).__init__()
        self.memory = deque([], maxlen=capacity)

    def push(self, *args):
        """Save a transition"""
        self.memory.append(Transition(*args))

    def sample(self, batch_size):
        return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)
    
    
class NLPAgent:
    def __init__(self, model_type="bert_gru", max_vocab_num=1000, update_freq=10, log_freq=1000, gamma=0.9, lr=1e-5):
        self.model_type = model_type
        self.max_vocab_num = max_vocab_num
        self.update_freq = update_freq
        self.log_freq = log_freq
        self.gamma = gamma
        self.lr = lr
        
        if torch.cuda.is_available():
            device = 'cuda'
        else:
            device = 'cpu'
            
        self.device = device
        
        self.glove = PretrainedEmbed.from_file("glove_300d.txt")
        self.idx2word = self.glove.words+["<PAD>", "<UNK>"]
        self.word2idx = {w: i for i, w in enumerate(self.idx2word)}
        
        if self.model_type == "gru":
            self.agent_model = AgentNetwork(len(self.idx2word), 300, 128, self.device).to(device)
            self.target_model = AgentNetwork(len(self.idx2word), 300, 128, self.device).to(device)
            self.target_model.load_state_dict(self.agent_model.state_dict())
            self.memory = ReplayMemory(10000)
        elif self.model_type == "gpt-2":
            self.agent_model = GPTNetwork(len(self.idx2word), 300, 128, self.device).to(device)
        elif self.model_type == 'bert_gru':
            self.agent_model = BERT_GRU(self.max_vocab_num, 128, self.device).to(device)
        self.optimizer = optim.Adam(self.agent_model.parameters(), lr=self.lr)
        
    def test(self):
        self.run_mode = "test"
        if self.model_type == "gru":
            self.agent_model.init_hidden(1)
        
    def train(self):
        self.agent_model.load_pretrained_embeddings(self.glove)
        self.run_mode = "train"
        if self.model_type == "gru":
            self.agent_model.init_hidden(1)
        
        self.stats = {"scores": [], "rewards": [], "policy": [], "values": [], "entropy": [], "confidence": []}
        self.replay_buffer = []
        self.last_score = 0
        self.num_step_train = 0
        
    def get_env_infos(self):
        return textworld.EnvInfos(admissible_commands=True, max_score = True, description=True, inventory=True, won=True, lost=True)
    
    def _tokenize_text(self, texts):
        texts = re.sub(r"[^a-zA-Z0-9\- ]", " ", texts)
        words_list = texts.split()
        words_idx = []
        for word in words_list:
            if word not in self.word2idx:
                words_idx.append(self.word2idx["<UNK>"])
            else:
                words_idx.append(self.word2idx[word])         
            
        return words_idx
    
    def _preprocess_texts(self, texts):
        tokenized_texts = []
        max_len = 0
        for text in texts:
            tokens = self._tokenize_text(text)
            tokenized_texts.append(tokens)
            max_len = max(max_len, len(tokens))

        padded = np.ones((len(tokenized_texts), max_len)) * self.word2idx["<PAD>"]

        for i, text in enumerate(tokenized_texts):
            padded[i, :len(text)] = text

        padded_tensor = torch.from_numpy(padded).type(torch.long).to(self.device)
        padded_tensor = padded_tensor.permute(1, 0) # Not batch first
        return padded_tensor
    
    def _discount_rewards(self, last_values):
        returns, advantages = [], []
        r = last_values.data
        for i in reversed(range(len(self.replay_buffer))):
            rewards, _, _, values = self.replay_buffer[i]
            r = rewards + self.gamma * r
            returns.append(r)
            advantages.append(r - values)
            
        return returns[::-1], advantages[::-1]
    
    def action(self, observations, score, done, infos):
        
        #If using GRU_BERT model, observations, descriptions, inventory, and commands are processed seperately
        if self.model_type == 'bert_gru':
            
            tokenizer = DistilBertTokenizer.from_pretrained('distilbert-base-uncased')
            
            ############ NOTE: distilBERT can only accept 512 tokens at a time. We are truncating all inputs to adhere to that length.
            ############ If inputs are significantly longer, performance may suffer
            observations_input = tokenizer(observations, 
                                           return_tensors='pt', 
                                           truncation=True,
                                           max_length=512,
                                           padding='max_length').to(self.device)
            descriptions_input = tokenizer(infos['description'],
                                           return_tensors='pt', 
                                           truncation=True,
                                           max_length=512,
                                           padding='max_length').to(self.device)
            inventory_input = tokenizer(infos['inventory'],
                                        return_tensors='pt', 
                                        truncation=True,
                                        max_length=512,
                                        padding='max_length').to(self.device)
            commands_input = tokenizer(infos["admissible_commands"],
                                       return_tensors='pt', 
                                       truncation=True,
                                       max_length=512,
                                       padding='max_length').to(self.device)
            #print('commands input: ', commands_input)
            output, index, value = self.agent_model(observations_input,
                                                    descriptions_input,
                                                    inventory_input,
                                                    commands_input)
        
            action_step = infos["admissible_commands"][index[0]]
        
        else:
            agent_input = "{}\n{}\n{}".format(observations, infos["description"], infos["inventory"])
            agent_input_tensor = self._preprocess_texts([agent_input]).to(self.device)
            commands_tensor = self._preprocess_texts(infos["admissible_commands"]).to(self.device)
            
            output, index, value = self.agent_model(agent_input_tensor, commands_tensor)
            action_step = infos["admissible_commands"][index[0]]
        
        # Test Mode, return action_step directly
        if self.run_mode == "test":
            if done and self.model_type == "gru":
                self.agent_model.init_hidden(1)
            return action_step
        
        # Train Mode
        self.num_step_train += 1
        
        if self.replay_buffer:
            reward = score - self.last_score 
            self.last_score = score
            if infos["won"]:
                reward += 100
            if infos["lost"]:
                reward -= 100
                
            self.replay_buffer[-1][0] = reward
        
        self.stats["scores"].append(score)
        
        # Update agent_model
        if self.num_step_train % self.update_freq == 0:
            returns, advantages = self._discount_rewards(value)
            
            loss = 0
            for infos_update, r, advantage in zip(self.replay_buffer, returns, advantages):
                reward, indexes, outputs, values = infos_update
                
                advantage        = advantage.detach()
                probs            = F.softmax(outputs, dim=2)
                log_probs        = torch.log(probs)
                log_action_probs = log_probs.gather(2, indexes)
                policy_loss      = (-log_action_probs * advantage).sum()
                value_loss       = (.5 * (values - r) ** 2.).sum()
                entropy     = (-probs * log_probs).sum()
                loss += policy_loss + 0.5 * value_loss - 0.1 * entropy
                
                self.stats["rewards"].append(reward)
                self.stats["policy"].append(policy_loss.item())
                self.stats["values"].append(value_loss.item())
                self.stats["entropy"].append(entropy.item())
                self.stats["confidence"].append(torch.exp(log_action_probs).item())
            
            if self.num_step_train % self.log_freq == 0:
                print("Total step: {:6d}  reward: {:3.3f}  value: {:3.3f}  entropy: {:3.3f}  max_score: {:3d}  num_vocab: {}".format(self.num_step_train, np.mean(self.stats["rewards"]), np.mean(self.stats["values"]), np.mean(self.stats["entropy"]), np.max(self.stats["scores"]), len(self.idx2word)))
                self.stats = {"scores": [], "rewards": [], "policy": [], "values": [], "entropy": [], "confidence": []}
            
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.agent_model.parameters(), 40)
            self.optimizer.step()
            self.optimizer.zero_grad()
        
            self.replay_buffer = []
            if self.model_type == "gru":
                self.agent_model.init_hidden(1)
        else:
            self.replay_buffer.append([None, index, output, value])
        
        if done:
            self.last_score = 0 
        
        return action_step
    
    def epsilon_greedy_action_selection(self, epsilon, agent_input_tensor, commands_tensor, infos, done):
        
        if np.random.random() > epsilon or self.run_mode == "test":
            agent_input_tensor = agent_input_tensor.to(self.device)
            commands_tensor = commands_tensor.to(self.device)
            
            output, index, value = self.agent_model(agent_input_tensor, commands_tensor)
            action_step = infos["admissible_commands"][index[0]]
            if self.run_mode == "test" and done and self.model_type == "gru":
                self.agent_model.init_hidden(1)
            
        else:
            action_step = np.random.choice(infos["admissible_commands"]) # Select random action with probability epsilon
        return action_step
    
    def replay(self, batch_size, gamma=0.5):
        if len(self.replay_buffer) < batch_size: 
            return
        transitions = self.memory.sample(batch_size)
        batch = Transition(*zip(*transitions))
        non_final_mask = torch.tensor(tuple(map(lambda s: s is not None, batch.next_observation)), device=self.device, dtype=torch.bool)
        non_final_next_observations = torch.cat([s for s in batch.next_observation if s is not None])
        non_final_next_commands = torch.cat([s for s in batch.next_commands if s is not None])
        
        observation_batch = torch.cat(batch.observation)
        commands_batch = torch.cat(batch.commands)
        action_batch = torch.cat(batch.action)
        reward_batch = torch.cat(batch.reward) 
        
        state_action_values = self.agent_model(observation_batch, commands_batch)[0].gather(1, action_batch)
        
        next_state_values = torch.zeros(batch_size, device=self.device)
        
        with torch.no_grad():
            next_state_values[non_final_mask] = self.target_model(non_final_next_observations, non_final_next_commands)[0].max(1)[0]
        
        expected_state_action_values = (next_state_values * gamma) + reward_batch
        criterion = torch.nn.SmoothL1Loss()
        loss = criterion(state_action_values, expected_state_action_values.unsqueeze(1))
        
        self.optimizer.zero_grad()
        loss.backward()
        
        torch.nn.utils.clip_grad_value_(self.agent_model.parameters(), 100)
        self.optimizer.step()
        
    
    def update_model_handler(self, epoch, update_target_model):
        if epoch > 0 and epoch % update_target_model == 0:
            self.target_model.load_state_dict(self.agent_model.state_dict())