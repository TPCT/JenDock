// expect: line 6: expression conditions
pipeline {
    agent any
    stages {
        stage('A') {
            when { expression { return true } }
            steps { echo 'x' }
        }
    }
}
