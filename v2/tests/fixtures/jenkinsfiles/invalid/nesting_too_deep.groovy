// expect: line 4: deeper than 32
pipeline { agent any
  stages { stage('A') { steps {
retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { retry(2) { echo 'x' } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } }
} } } }
